# syntax=docker/dockerfile:1.6
#
# Smoke-test image for the logoscore daemon. Builds entirely inside a
# Linux container — no cross-compilation from the host. Docker Desktop
# on macOS runs this via the host-native Linux VM (linux/arm64 on
# Apple Silicon), so no Rosetta emulation is needed. Same pattern as
# status-go's tests-functional image.
#
# Usage (from this repo's root):
#   docker build -f tests/docker_smoke/Dockerfile -t logoscore:smoke .
#
#   # Build the "portable" flavor (cli-bundle-dir + install-portable):
#   docker build --build-arg FLAVOR=portable \
#       -f tests/docker_smoke/Dockerfile -t logoscore:smoke-portable .
#
# FLAVOR selects the flake's bundle output:
#   - dev       (default) → .#dockerBundle          — uses .#cli + .install
#   - portable            → .#dockerBundlePortable  — uses .#cli-bundle-dir + .install-portable
#
# The image uses whatever logos-logoscore-cli revision this repo's
# flake.lock / flake.nix points at. To iterate on unpublished CLI
# changes, push them to a branch and update the flake accordingly.

FROM nixos/nix:2.24.9 AS build

ARG FLAVOR=dev

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
# Runtime stage. Shape depends on flavor:
#   dev      → /usr/local/bin/logoscore links rpath into /nix/store, so
#              we copy the store. Image is 3+ GB.
#   portable → /opt/logoscore/{bin,lib,modules} is self-contained; no
#              nix store needed at runtime. Image is ~500 MB. We pick a
#              minimal debian base instead of nixos/nix.
# ─────────────────────────────────────────────────────────────────────

# Single runtime stage, using nixos/nix as a convenient common base.
# The dev flavor needs /nix/store available; the portable flavor
# doesn't, but keeping a single stage keeps the Dockerfile linear
# and the size difference is still dominated by the flavor's own
# content. If portable-image size becomes important, split this into
# two stages keyed off $FLAVOR with a multi-stage `FROM …AS rt-dev`
# / `FROM debian AS rt-portable` + a final `FROM rt-$FLAVOR`.

FROM nixos/nix:2.24.9
ARG FLAVOR=dev

# Stage in everything the bundle needs. For `dev` we need the full nix
# closure (binary links via rpath into /nix/store); for `portable` the
# bundle has its own bin/lib/modules and we only need those.
COPY --from=build /nix/store                 /nix/store
COPY --from=build /out/bundle                /opt/logoscore

# Wire a fixed launch path so the entrypoint doesn't need to know about
# the flavor — both layouts put the binary at the same relative path.
RUN ln -s /opt/logoscore/bin/logoscore /usr/local/bin/logoscore

# Default modules dir at /opt/logoscore/modules in both flavors.
EXPOSE 6000
ENTRYPOINT ["/usr/local/bin/logoscore"]
CMD ["daemon", \
     "--transport", "tcp", \
     "--tcp-host",  "0.0.0.0", \
     "--tcp-port",  "6000", \
     "-m",          "/opt/logoscore/modules"]
