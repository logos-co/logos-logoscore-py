# syntax=docker/dockerfile:1.6
#
# Reusable logosctl daemon image. Contains only the CLI and the modules it
# ships with (capability_module, modules_state, the peering modules, and
# the package modules); user modules are expected to be bind-mounted in at
# runtime. The daemon's config is a document installed into the session
# before it starts, and the host operates it over Remote Runtime Control:
#
#   docker run --rm -v "$PWD/config":/config -e LOGOSCTL_CONFIG_DIR=/config \
#       logosctl:smoke-portable daemon config set /config/daemon.yaml
#   docker run --network host \
#       -v "$PWD/config":/config -e LOGOSCTL_CONFIG_DIR=/config \
#       -v "$PWD/persistence":/persistence \
#       -v /path/to/your/modules:/user-modules:ro \
#       logosctl:smoke-portable daemon start
#
# with a daemon.yaml naming /user-modules and /persistence and a `peering`
# section with `runtime_control: true`. That is the shape
# `LogosctlDockerDaemon` (src/logosctl/docker_daemon.py) runs, pairing a
# host-side logosctl with it; consumers who want to test their own
# Qt-plugin modules against a real daemon should use it. Pre-seed
# `/persistence` on the host to restore a session; read it back after to
# inspect state.
#
# Builds entirely inside a Linux container — no cross-compilation from
# the host. Docker Desktop on macOS runs this via the host-native Linux
# VM (linux/arm64 on Apple Silicon), so no Rosetta emulation is needed.
# Same pattern as status-go's tests-functional image.
#
# Usage (from this repo's root):
#   docker build -f tests/docker_smoke/Dockerfile -t logosctl:smoke-portable .
#
#   # Build the "dev" flavor (the nix-store-linked ctl package):
#   docker build --build-arg FLAVOR=dev \
#       -f tests/docker_smoke/Dockerfile -t logosctl:smoke-dev .
#
# FLAVOR selects the flake's bundle output:
#   - portable (default) → .#dockerBundlePortable  — logosctl's ctl-bundle-dir
#   - dev                → .#dockerBundle          — logosctl's ctl package
#
# NIX_CONFIG adds nix.conf lines to the build stage, e.g. `cores = 8` on a
# shared machine (build_smoke_image.sh passes $SMOKE_NIX_CONFIG).
#
# The image uses whatever logos-logoscore-cli revision this repo's
# flake.lock / flake.nix points at. To iterate on unpublished CLI
# changes, push them to a branch and update the flake accordingly.

FROM nixos/nix:2.24.9 AS build

ARG FLAVOR=portable
# Nix reads it from the environment, which build args are in during RUN.
ARG NIX_CONFIG=""

# Enable flakes. Disable the sandbox + seccomp filter: Docker Desktop's
# Rosetta layer (on Apple Silicon's linux/amd64 fallback) blocks the
# seccomp BPF that nix's sandbox wants to install, and we don't need
# the sandbox anyway — the docker layer already isolates the build.
# The Logos Attic caches are anonymously readable and hold the CLI
# closure, so the `nix build` below substitutes instead of compiling;
# `fallback` keeps the build working when the cache is unreachable.
RUN mkdir -p /etc/nix && { \
        echo "experimental-features = nix-command flakes"; \
        echo "sandbox = false"; \
        echo "filter-syscalls = false"; \
        echo "extra-substituters = https://cache.nix.logos.co/public https://cache.nix.logos.co/ci"; \
        echo "extra-trusted-public-keys = public:l4HrXgL4nw246+LBh2SOJyhz64BoGegOYLheT/iIAPU= ci:aVJqjS4NWX5WfqHO0AEhIScjGu/JyK5FoydPrnEbMvc="; \
        echo "fallback = true"; \
    } > /etc/nix/nix.conf

WORKDIR /src
COPY flake.nix flake.lock pyproject.toml README.md ./
COPY src ./src

# Map FLAVOR → flake attribute.
RUN case "$FLAVOR" in \
        dev)      ATTR=dockerBundle         ;; \
        portable) ATTR=dockerBundlePortable ;; \
        *) echo "Unknown FLAVOR='$FLAVOR'; use dev|portable" >&2; exit 1 ;; \
    esac && \
    nix build -L ".#$ATTR" --out-link /out/bundle

# ─────────────────────────────────────────────────────────────────────
# Runtime stage.
#
# We use ubuntu:24.04 as the runtime base (rather than nixos/nix or
# alpine) because it ships a fully normal glibc runtime:
#   * `/lib/ld-linux-<arch>.so.1` exists at the canonical path, so the
#     portable bundle's ELF PT_INTERP resolves natively (the
#     `ctl-bundle-dir` binary isn't patched to use a custom interpreter).
#   * `libstdc++.so.6`, `libgcc_s.so.1`, `libssl.so.3`, `libcrypto.so.3`
#     etc. are all in standard locations, matching what the binary's
#     `$ORIGIN/../lib` rpath plus the dynamic linker's default search
#     paths expect. No LD_LIBRARY_PATH hacks, no symlinks from /nix/store.
#   * Standard Linux filesystem layout (`/usr/local/bin`, `/etc`, …),
#     which simplifies anything that writes there at image-build time.
#
# Same Ubuntu-as-runtime pattern used by
# https://github.com/logos-co/logos-docker. Builder stage stays on
# nixos/nix for the nix-build itself.
#
# Size: dev ≈ 3 GB (ubuntu base + nix closure), portable ≈ 700 MB
# (ubuntu base + Qt/boost/openssl libs bundled). If the dev size
# becomes painful we can split this into two runtime stages keyed off
# $FLAVOR; for now one linear Dockerfile is easier to reason about.
# ─────────────────────────────────────────────────────────────────────

FROM ubuntu:24.04
ARG FLAVOR=portable

# `ca-certificates` lets the package downloader verify the catalog's
# https server. Every other runtime dep (glibc, libstdc++, libssl,
# libcrypto) ships with ubuntu:24.04 out of the box.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# For `dev`, the binary links via rpath into /nix/store — we need to
# ship the closure. For `portable`, the binary links into $ORIGIN/../lib
# (shipped inside the bundle) and resolves standard libs via ubuntu's
# default linker search path, so /nix/store is dead weight — but
# copying it unconditionally keeps the Dockerfile linear and the layer
# is a no-op for callers who already have the image built.
COPY --from=build /nix/store                 /nix/store
COPY --from=build /out/bundle                /opt/logosctl

# Both flavors put the binary at the same path, with the built-in modules
# beside it (the daemon finds <bin>/../modules on its own).
ENTRYPOINT ["/opt/logosctl/bin/logosctl"]
# Default CMD = a daemon on whatever config its session holds; see the
# usage block at the top for installing one first.
CMD ["daemon", "start"]
