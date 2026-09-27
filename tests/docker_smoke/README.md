# Docker smoke tests for `logosctl`

These tests are opt-in: they spawn real docker containers running a
logosctl daemon and operate them from the host over Remote Runtime Control.
Without docker, or without the image, they skip cleanly; the rest of the
suite stays green on runners that don't have docker available.

## The image is a reusable CLI runtime

The `logosctl:smoke-*` image contains **only** logosctl plus the modules
it ships with (`capability_module`, `modules_state`, `peering_module`,
`peering_identity`, and the package modules). It does NOT bake in
`test_fullapi_cpp` or any other user module. User modules — the ones
you're writing and testing — are bind-mounted in at runtime.

### Preferred: `LogosctlDockerDaemon`

If you're testing a module from Python, use the helper that ships with the
`logosctl` package. It encapsulates the container lifecycle (the config
document, volume mounts, pairing the host client) so your tests don't have
to:

```python
from logosctl import LogosctlDockerDaemon

with LogosctlDockerDaemon(
    image="logosctl:smoke-portable",
    modules_dir="./my-module/result/modules",  # host path
    binary="logosctl",                         # host-side client
) as daemon:
    client = daemon.client()
    client.load_module("my_module")
    print(client.call("my_module", "do_something", 42))
```

`start()` installs the daemon's config with a throwaway `daemon config set`
container, runs the daemon with `--network host`, waits for `state.json`,
then pairs the host-side logosctl (a config dir of its own, no daemon):

1. `logosctl peer invite --runtime-control` inside the container
   (`docker exec`), as the daemon's local operator;
2. `logosctl remote pair -` on the host, the invite on its stdin;
3. `logosctl peer accept <id>` inside the container, for the pending
   pairing from the host client's runtime ID;
4. `logosctl peer policy set /config/remote-policy.json` inside the
   container, granting the client `DEFAULT_GRANTS` (or `grants=`).

`client()` returns a `LogosctlClient` that runs every command with
`--remote`; `daemon.runtime_control.grant({...})` changes what it may call.
Optional knobs: `control_port=...` to pin the control endpoint's port,
`persistence_dir=...` to restore a pre-seeded session, `name=...` for the
daemon's peering name, `extra_module_dirs=[...]` / `extra_config={...}` /
`extra_args=[...]` to extend the daemon's setup.

### Host networking

The daemon's control endpoint listens on a fixed port on 127.0.0.1, and
core_service's runtime-control listener (`tls_tcp`) on a port the daemon
picks. The container shares the host's network, so the host client
reaches both on its own loopback with no port mapping — which is also why
there is no `-p`, and why two containers only need distinct control ports.
That is complete on Linux. Docker Desktop has host networking as an opt-in
setting (Settings → Resources → Network).

### Mounts

| Host dir               | Container path  | Read/write | Why                                                                                   |
|------------------------|-----------------|------------|---------------------------------------------------------------------------------------|
| session dir            | `/config`       | rw         | The daemon's session (`daemon.yaml`, `remote-policy.json`, `daemon/`, `peering/`, …). |
| persistence dir        | `/persistence`  | rw         | Module state (`persistence_path`). Pre-seed to restore a session; read back after.    |
| your modules dir       | `/user-modules` | ro         | Compiled Qt plugins, named in `modules_dirs`. The daemon never mutates these.         |

The daemon writes `/config` as root; read its files through the container
(`read_container_file`, `state_json`, `daemon_log`), not from the host.

## Flavors

Two build flavors, to match how the daemon gets distributed:

| Flavor     | Flake attr               | Binary                                       |
|------------|--------------------------|----------------------------------------------|
| `portable` | `.#dockerBundlePortable` | logosctl's `ctl-bundle-dir` (self-contained) |
| `dev`      | `.#dockerBundle`         | logosctl's `ctl` package (nix-store rpaths)  |

**`portable` is the default** — the self-contained `bin/ + lib/ +
modules/` tree that matches how released binaries are distributed, so
it's the most realistic smoke. `dev` links against Qt/Boost/OpenSSL via
nix-store rpaths and requires copying `/nix/store` into the image at build
time. The mounted modules are `.install-portable` builds, which load in
either.

## Setup

```bash
# Build one flavor (default: portable)
./tests/docker_smoke/build_smoke_image.sh
FLAVOR=dev      ./tests/docker_smoke/build_smoke_image.sh
FLAVOR=both     ./tests/docker_smoke/build_smoke_image.sh    # builds both
# On a shared machine, cap the in-docker nix build:
SMOKE_NIX_CONFIG=$'cores = 8\nmax-jobs = 2' ./tests/docker_smoke/build_smoke_image.sh

# Run the suite (default: portable). `nix develop` provides logosctl and,
# on Linux, LOGOSCTL_DOCKER_MODULES_DIR.
nix develop --command pytest tests/docker_smoke
nix develop --command pytest tests/docker_smoke --docker-flavor=both
```

Tag convention: `logosctl:smoke-dev` / `logosctl:smoke-portable`.
Override with `LOGOSCTL_DOCKER_IMAGE_FMT='myimg:{flavor}'` if you publish
elsewhere. The host client is `LOGOSCTL_BIN` (or `logosctl` on PATH); it
may be a macOS build, since it only speaks `tls_tcp` to the container.

The image is built by `docker build` from a multi-stage Dockerfile whose
first stage runs `nix build` *inside* a `nixos/nix` Linux container.
Because everything happens inside Docker, the host never needs to
cross-compile — Docker Desktop on macOS uses its native Linux VM
(linux/arm64 on Apple Silicon), same pattern as
[status-go](https://github.com/status-im/status-go/tree/develop/tests-functional).

Build context: only the `logos-logoscore-py` repo. The flake pulls
`logos-logoscore-cli` from github at the revision this repo's
`flake.nix` / `flake.lock` references. To iterate on unpublished CLI
changes, push them to a branch and bump `logos-logoscore-cli.url` in
`flake.nix`:

```nix
logos-logoscore-cli.url = "github:<you>/logos-logoscore-cli/<branch>";
```

## What's covered

1. **Every echo method on `test_fullapi_cpp`**, through the pairing: `tstr`,
   `bstr` (canonical `{"_bytes"}` tag), `int`, `uint`, `float64`, `bool`,
   `any`, the typed arrays (`[tstr]/[int]/[uint]/[float64]/[bool]`), `[any]`
   (LogosList), `{tstr:any}` (LogosMap), `result`, and `void`.

2. **Every typed event** — one per event-legal type (`stringEvent`,
   `bytesEvent`, `intEvent`, … `mapEvent`), fired via the module's
   `fire<X>Event(v)` triggers and watched with `logosctl --remote … watch`.

3. **The daemon's remote policy decides**: an ungranted method is refused
   with `NOT_AUTHORISED`, and a new grant holds from the next call.

4. **Two independent daemons** in two containers, one paired client each:
   distinct instance and runtime IDs, and a module loaded on A is not
   loaded on B.

The legacy tcp / tcp_ssl smoke (and its JSON/CBOR codec matrix) went with
those transports.

### Building your module for the container

The daemon image is Linux. Your module's compiled plugin needs to be a
Linux `.so` with a glibc/Qt/OpenSSL ABI compatible with the image's
runtime. Modules built on macOS (dylibs) won't load; modules built on
Linux with a different glibc usually won't either.

On Linux, `nix build .#install-portable` in your module's flake is enough
(the smoke itself mounts the dev shell's `LOGOSCTL_DOCKER_MODULES_DIR`).
Elsewhere, use the helper that builds inside the same nixos/nix base the
daemon image was compiled in:

```python
from logosctl import LogosctlDockerDaemon, build_modules_in_docker

modules_dir = build_modules_in_docker(
    builds=[
        # Each entry is (flake_ref, attr). ALL builds share one container
        # run / one nix store, so common deps (logos-cpp-sdk, Qt, boost,
        # openssl) get fetched once.
        ("github:user/my-module",  "packages.x86_64-linux.install-portable"),
        ("github:user/my-module2", "packages.x86_64-linux.install-portable"),
    ],
    output_dir="./build/modules",
)

with LogosctlDockerDaemon(
    image="logosctl:smoke-portable",
    modules_dir=modules_dir,
) as daemon:
    client = daemon.client()
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
Without `LOGOSCTL_DOCKER_MODULES_DIR`, the smoke builds `test_fullapi_cpp`
this way, from the logos-test-modules revision `flake.lock` pins
(`LOGOSCTL_TEST_MODULES_FLAKE` overrides it).
