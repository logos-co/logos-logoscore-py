# Docker smoke tests for `logoscore`

These tests are opt-in: they spawn real docker containers running the
logoscore daemon and drive them via the Python wrapper from the host.
Without docker installed they skip cleanly; the rest of the suite stays
green on CI runners that don't have docker available.

## The image is a reusable CLI runtime

The `logoscore:smoke-*` image contains **only** the logoscore CLI plus
the modules the CLI itself ships with (`capability_module`,
`package_manager_module`). It does NOT bake in `test_basic_module` or
any other user module. User modules — the ones you're writing and
testing — are bind-mounted in at runtime.

### Preferred: `LogoscoreDockerDaemon`

If you're testing a module from Python, use the helper that ships with
`logoscore-py`. It encapsulates the container lifecycle (volume mounts,
port wiring, client construction) so your tests don't have to:

```python
from logoscore import LogoscoreDockerDaemon

with LogoscoreDockerDaemon(
    image="logoscore:smoke-portable",
    modules_dir="./my-module/result/modules",  # host path
) as daemon:
    client = daemon.client(binary="logoscore")
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

The helper picks a free host port, bind-mounts everything the daemon
needs (`/config`, `/persistence`, `/user-modules`), starts the
container, waits for `daemon.json`, and returns a `LogoscoreClient`
configured to dial the right port. Optional knobs: `host_port=...` to
pin a port, `persistence_dir=...` to restore a pre-seeded session,
`codec="cbor"` to pick a wire codec, `extra_module_dirs=[...]` /
`extra_args=[...]` to extend the daemon invocation.

### Equivalent raw `docker run`

For reference / non-Python callers the same thing as a shell invocation:

```bash
docker run --rm -p 6000:6000 \
    -v "$PWD/config":/config \
    -v "$PWD/persistence":/persistence \
    -v "$PWD/my-modules-install/modules":/user-modules:ro \
    logoscore:smoke-portable \
    daemon --config-dir /config \
           --persistence-path /persistence \
           --transport tcp --tcp-host 0.0.0.0 --tcp-port 6000 \
           -m /opt/logoscore/modules \
           -m /user-modules
```

The three mounts each have a specific purpose:

| Host dir                  | Container path | Read/write | Why                                                                                           |
|---------------------------|----------------|------------|-----------------------------------------------------------------------------------------------|
| `./config`                | `/config`      | rw         | Daemon writes `daemon.json` here; your client reads it to discover host/port/instance_id.     |
| `./persistence`           | `/persistence` | rw         | Module state (`--persistence-path`). Pre-seed to restore a session; read back to inspect it.  |
| `./my-modules/modules`    | `/user-modules`| ro         | Compiled Qt plugins loaded via `-m`. Read-only because the daemon never mutates these.        |

### Port strategy

The container always binds `6000` internally; the host maps a
dynamically-picked ephemeral port to it (`-p $host_port:6000`). The
client gets told `tcp_port=$host_port` so it dials `localhost:$host_port`
rather than the container-internal `6000` the daemon wrote into its
`daemon.json`. Same pattern as
[status-go tests-functional](https://github.com/status-im/status-go/tree/develop/tests-functional).

Result: parallel container-backed tests don't fight over port 6000 on
the host, and you don't need to know which ports are free before you
start.

## Flavors

Two build flavors, to match how the daemon gets distributed:

| Flavor     | Flake attr               | Binary                                | Modules the user mounts in     | Image size |
|------------|--------------------------|---------------------------------------|--------------------------------|------------|
| `portable` | `.#dockerBundlePortable` | `…cli-bundle-dir` (self-contained)    | `.install-portable`            | ~600 MB    |
| `dev`      | `.#dockerBundle`         | `logos-logoscore-cli.packages.…cli`   | `.install` (nix-store rpaths)  | ~3 GB      |

**`portable` is the default** — it's the self-contained
`bin/ + lib/ + modules/` tree that matches how released logoscore
binaries are distributed, so it's the most realistic smoke. `dev`
links against Qt/Boost/OpenSSL via nix-store rpaths (what the
`logoscore-py` dev shell itself uses) and is faster to iterate on when
you already have the nix cache warm, but requires copying `/nix/store`
into the image at build time.

The user-mounted modules must match the image flavor: `.install`
modules (rpath-linked into `/nix/store`) only work in the `dev` image
because its `/nix/store` is present; `.install-portable` modules
(self-contained shared-lib bundles) work in the `portable` image.
The smoke test driver picks the right one via
`LOGOSCORE_TEST_MODULES_DIR` (dev) or
`LOGOSCORE_TEST_MODULES_DIR_PORTABLE` (portable), both set by the
`nix develop` shell.

## Setup

```bash
# Build one flavor (default: dev)
./build_smoke_image.sh
FLAVOR=portable ./build_smoke_image.sh
FLAVOR=both     ./build_smoke_image.sh         # builds both

# Run the suite (default: dev)
pytest tests/docker_smoke --docker-flavor=dev
pytest tests/docker_smoke --docker-flavor=portable
pytest tests/docker_smoke --docker-flavor=both    # replays matrix twice
```

Tag convention: `logoscore:smoke-dev` / `logoscore:smoke-portable`.
Override with `LOGOSCORE_DOCKER_IMAGE_FMT='myimg:{flavor}'` if you
publish elsewhere.

The image is built by `docker build` from a multi-stage Dockerfile
whose first stage runs `nix build` *inside* a `nixos/nix` Linux
container. Because everything happens inside Docker, the host never
needs to cross-compile — Docker Desktop on macOS uses its native Linux
VM (linux/arm64 on Apple Silicon), same pattern as
[status-go](https://github.com/status-im/status-go/tree/develop/tests-functional).

Build context: only the `logos-logoscore-py` repo. The flake pulls
`logos-logoscore-cli` and `logos-test-modules` from github at the
revisions this repo's `flake.nix` / `flake.lock` references. To
iterate on unpublished CLI changes, push them to a branch and bump
`logos-logoscore-cli.url` in `flake.nix`:

```nix
logos-logoscore-cli.url = "github:<you>/logos-logoscore-cli/<branch>";
```

then rebuild the image:

```bash
./tests/docker_smoke/build_smoke_image.sh
```

First build takes a few minutes while nix populates its store in the
builder layer; subsequent builds are incremental thanks to Docker's
layer cache and nix's content-addressed store.

## What's covered

1. **Every Q_INVOKABLE on `test_basic_module`** replayed through the full
   wire stack — runs the matrix twice, once with `--tcp-codec=json` and
   once with `--tcp-codec=cbor`, so both codecs see every parameter /
   return type (void, bool, int, QString, QVariantMap, QJsonArray,
   QStringList, QByteArray, QUrl, LogosResult).

2. **Both events** — `testEvent` (single-arg payload) and `multiArgEvent`
   (QString + int). Under each codec. Also validates the `logoscore
   watch` subprocess plumbing end-to-end over TCP.

3. **Two independent daemons** running in two separate containers on two
   host ports, driven from one Python test. Confirms:
   - distinct instance_ids in each container's `daemon.json`
   - a module loaded on A isn't visible to B
   - a call to A succeeds; the same call to B fails because the module
     isn't loaded there
   That covers the "one test talks to two daemons" need without needing
   a real multi-host setup.

4. **Legacy smoke** (`test_docker_tcp_status`, `test_docker_tcp_load_and_call`)
   — kept as a minimal fallback. Useful when the matrix fixtures skip
   for environmental reasons; always worth running on top of anything
   else.

5. **TLS smoke** (`test_docker_ssl_smoke.py`) — one daemon inside docker
   listening on `tcp_ssl` with a self-signed cert generated at fixture
   time (via `openssl req`) and bind-mounted into the container. The
   host-side client dials over TLS with `--no-verify-peer` (because
   self-signed) and runs two scenarios: a `status` round-trip (with a
   `rpc_error not in response` assertion so it doesn't false-positive
   on the fallback-to-daemon.json path when the RPC actually fails)
   and a load-module + method-call round-trip (proves the full
   payload path — method args → TLS → RPC → return → TLS → client).

Per-test skips show exactly which of (json, cbor, two-daemon, ssl)
you're missing when docker or `openssl` isn't present.

### Building your module for the container

The daemon image is Linux. Your module's compiled plugin needs to be a
Linux `.so` with a glibc/Qt/OpenSSL ABI compatible with the image's
runtime. Modules built on macOS (dylibs) won't load; modules built on
Linux with a different glibc usually won't either.

Use the helper that builds inside the same nixos/nix base the daemon
image was compiled in, guaranteeing ABI compatibility regardless of
host OS:

```python
from logoscore import LogoscoreDockerDaemon, build_modules_in_docker

modules_dir = build_modules_in_docker(
    builds=[
        # Each entry is (flake_ref, attr). ALL builds share one container
        # run / one nix store, so common deps (logos-cpp-sdk, Qt, boost,
        # openssl) get fetched once. Adding a second module costs only
        # its own compile time, not another full closure download.
        ("github:user/my-module",  "packages.x86_64-linux.install-portable"),
        ("github:user/my-module2", "packages.x86_64-linux.install-portable"),
    ],
    output_dir="./build/modules",
)

with LogoscoreDockerDaemon(
    image="logoscore:smoke-portable",
    modules_dir=modules_dir,
) as daemon:
    client = daemon.client(binary="logoscore")
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

Or via the shell wrapper at `tests/docker_smoke/build_modules_in_docker.sh`:

```bash
./tests/docker_smoke/build_modules_in_docker.sh ./build/modules \
    'github:user/my-module#packages.x86_64-linux.install-portable' \
    'github:user/my-module2#packages.x86_64-linux.install-portable'
```

Each `attr` must point at a derivation whose output contains a
`modules/<name>/<plugin>.so + manifest.json` tree. The standard
`logos-module-builder` `.install-portable` output produces exactly this.

### Using `LogoscoreDockerDaemon` for TLS directly

```python
from logoscore import LogoscoreDockerDaemon

with LogoscoreDockerDaemon(
    image="logoscore:smoke-portable",
    modules_dir="./my-modules/modules",
    transport="tcp_ssl",
    ssl_cert=Path("./cert.pem"),   # host paths — mounted as /certs/*.pem
    ssl_key=Path("./key.pem"),
) as daemon:
    client = daemon.client(binary="logoscore")   # auto-passes --no-verify-peer
    client.status()
```

The helper bind-mounts the two PEM files as individual files into
`/certs/cert.pem` and `/certs/key.pem` (so they can live in different
host dirs) and flags the daemon with `--transport=local --transport=tcp_ssl
--tcp-ssl-port=<internal> --ssl-cert=... --ssl-key=...`. `local` is kept
alongside `tcp_ssl` so module-to-module traffic inside the daemon process
group still uses the local socket; without it, spawned module-host
processes would inherit the tcp_ssl default and abort with "TLS bind
failed" (they have no cert to bind with).
