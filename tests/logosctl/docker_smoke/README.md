# Docker smoke tests for `logosctl`

These tests are opt-in: they spawn real docker containers running the
logosctl daemon and drive them via the Python wrapper from the host.
Without docker, the image, or a `logosctl` binary (on PATH or in
`LOGOSCTL_BIN`) they skip cleanly; the TLS test also needs `openssl`.

## The image

`logosctl:smoke-portable` contains only the logosctl CLI plus the modules
it bundles (`capability_module`, `modules_state` and the package-management
modules), under `/opt/logosctl`, with `logosctl` as the entrypoint. It is
built from `.#logosctlDockerBundlePortable`, a self-contained bundle: stage
1 of the `Dockerfile` runs `nix build` inside `nixos/nix:2.24.9`, and the
runtime stage is `ubuntu:24.04`. There is no `dev` flavor:
`--docker-flavor=portable` (the default) and `both` run the suite against
this image, `dev` selects nothing.

User modules are bind-mounted in at `/user-modules` by
`LogosctlDockerDaemon` (`src/logosctl/docker_daemon.py`), which also
installs the daemon's config document into the session (a throwaway
`daemon config set /config/daemon.yaml` container) before starting the
daemon. Inside the container the session is `/config`, persistence is
`/persistence`, and for `tcp_ssl` the cert and key are mounted at
`/certs/cert.pem` / `/certs/key.pem`. `core_service` listens on 6000 and
`capability_module` on 6001; each is forwarded to its own free host port.

## Build and run

```bash
tests/logosctl/docker_smoke/build_smoke_image.sh

# Run the suite
pytest tests/logosctl/docker_smoke
nix develop --command pytest tests/logosctl/docker_smoke -v
```

The build script honours `PLATFORM` (passed to `--platform`; default is
the host's native Linux platform) and `BUILDX_CACHE_FROM` /
`BUILDX_CACHE_TO` (buildx cache backends; CI uses `type=gha`).

The suite builds `test_fullapi_cpp` (`.install-portable`) inside docker
once per session with `build_modules_in_docker`, from
`github:logos-co/logos-test-modules`. Override that flake with
`LOGOSCTL_TEST_MODULES_FLAKE` and the image tag with
`LOGOSCTL_DOCKER_IMAGE_FMT='myimg:{flavor}'`.

The suite covers every echo method and typed event of `test_fullapi_cpp`
over both wire codecs, two independent daemons, a shared docker network,
env vars reaching the daemon process, and the `tcp_ssl` transport.
