# Docker smoke tests for `logoscore`

These tests are opt-in: they spawn real docker containers running the
logoscore daemon and drive them via the Python wrapper from the host.
Without docker installed they skip cleanly; the rest of the suite stays
green on CI runners that don't have docker available.

## Flavors

Two build flavors, to match how the daemon gets distributed:

| Flavor     | Flake attr               | Binary                              | Test modules            | Image size |
|------------|--------------------------|-------------------------------------|-------------------------|------------|
| `dev`      | `.#dockerBundle`         | `logos-logoscore-cli.packages.…cli` | `.test_basic_module.install`          | ~3 GB      |
| `portable` | `.#dockerBundlePortable` | `…cli-bundle-dir` (self-contained)  | `.test_basic_module.install-portable` | ~600 MB    |

The `dev` flavor links against Qt/Boost/OpenSSL via nix-store rpaths —
what the `logoscore-py` dev shell uses. The `portable` flavor is a
self-contained bin/+lib/+modules/ tree that matches a released
logoscore distribution.

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

Per-test skips show exactly which of (json, cbor, two-daemon) you're
missing when docker isn't present.
