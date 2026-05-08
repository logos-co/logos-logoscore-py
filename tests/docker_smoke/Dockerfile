# syntax=docker/dockerfile:1.6
#
# Reusable logoscore daemon image. Contains only the CLI (and the modules
# it ships with — capability_module, package_manager_module); user modules
# are expected to be bind-mounted in at runtime:
#
#   docker run --rm -p 6000:6000 \
#       -v "$PWD/config":/config \
#       -v "$PWD/persistence":/persistence \
#       -v /path/to/your/modules:/user-modules:ro \
#       logoscore:smoke-portable \
#       daemon --config-dir /config \
#              --persistence-path /persistence \
#              --transport tcp --tcp-host 0.0.0.0 --tcp-port 6000 \
#              -m /opt/logoscore/modules \
#              -m /user-modules
#
# That's the exact shape the pytest smoke suite uses and what
# logoscore-py consumers (who want to test their own Qt-plugin modules
# against a real daemon) should replicate. Pre-seed `/persistence` on
# the host to restore a session; read it back after to inspect state.
#
# Builds entirely inside a Linux container — no cross-compilation from
# the host. Docker Desktop on macOS runs this via the host-native Linux
# VM (linux/arm64 on Apple Silicon), so no Rosetta emulation is needed.
# Same pattern as status-go's tests-functional image.
#
# Usage (from this repo's root):
#   docker build -f tests/docker_smoke/Dockerfile -t logoscore:smoke .
#
#   # Build the "portable" flavor (cli-bundle-dir):
#   docker build --build-arg FLAVOR=portable \
#       -f tests/docker_smoke/Dockerfile -t logoscore:smoke-portable .
#
# FLAVOR selects the flake's bundle output:
#   - dev       (default) → .#dockerBundle          — uses .#cli
#   - portable            → .#dockerBundlePortable  — uses .#cli-bundle-dir
#
# The image uses whatever logos-logoscore-cli revision this repo's
# flake.lock / flake.nix points at. To iterate on unpublished CLI
# changes, push them to a branch and update the flake accordingly.

FROM nixos/nix:2.24.9 AS build

ARG FLAVOR=portable

# Enable flakes. Disable the sandbox + seccomp filter: Docker Desktop's
# Rosetta layer (on Apple Silicon's linux/amd64 fallback) blocks the
# seccomp BPF that nix's sandbox wants to install, and we don't need
# the sandbox anyway — the docker layer already isolates the build.
RUN mkdir -p /etc/nix && { \
        echo "experimental-features = nix-command flakes"; \
        echo "sandbox = false"; \
        echo "filter-syscalls = false"; \
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
#     `cli-bundle-dir` binary isn't patched to use a custom interpreter).
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

# `ca-certificates` lets the TLS transport verify server certs against
# the system CA bundle when `--no-verify-peer` isn't in play. Every
# other runtime dep (glibc, libstdc++, libssl, libcrypto) ships with
# ubuntu:24.04 out of the box.
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
COPY --from=build /out/bundle                /opt/logoscore

# Both flavors put the binary at the same path, so the entrypoint can
# point straight at it.
EXPOSE 6000
ENTRYPOINT ["/opt/logoscore/bin/logoscore"]
# Default CMD = a bare daemon scanning only the CLI's built-in modules.
# In practice callers almost always override CMD to add their own `-m
# /user-modules` (or equivalent) — see the usage block at the top.
CMD ["daemon", \
     "--transport", "tcp", \
     "--tcp-host",  "0.0.0.0", \
     "--tcp-port",  "6000", \
     "-m",          "/opt/logoscore/modules"]
