# Docker smoke tests for `logosctl`

These tests are opt-in: they spawn real docker containers running the
logosctl daemon and drive them via the Python wrapper from the host.
Without docker, the image, or a `logosctl` binary (on PATH or in
`LOGOSCTL_BIN`) they skip cleanly.

## The image

`logosctl:smoke-*` contains only the logosctl CLI plus the modules it
ships with (`capability_module`, `package_manager_module`). User modules
are bind-mounted in at `/user-modules` by `LogosctlDockerDaemon`
(`src/logosctl/docker_daemon.py`), which also installs the daemon's
config document into the session before starting the daemon.

The image is built from `.#logosctlDockerBundlePortable`, a self-contained
bundle, and tagged `logosctl:smoke-portable`. There is no `dev` flavor.

## Build and run

```bash
tests/logosctl/docker_smoke/build_smoke_image.sh

# Run the suite
pytest tests/logosctl/docker_smoke
nix develop --command pytest tests/logosctl/docker_smoke -v
```

Override the image tag with `LOGOSCTL_DOCKER_IMAGE_FMT='myimg:{flavor}'`
and the test-modules flake with `LOGOSCTL_TEST_MODULES_FLAKE`.

The suite covers every echo method and typed event of `test_fullapi_cpp`
over both wire codecs, two independent daemons, a shared docker network,
env vars reaching the daemon process, and the `tcp_ssl` transport.
