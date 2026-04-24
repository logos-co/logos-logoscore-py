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
# Runtime stage. Shape depends on flavor:
#   dev      → /opt/logoscore/bin/logoscore is rpath-linked into /nix/store,
#              so we copy the store. Image is 3+ GB.
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
ARG FLAVOR=portable

# Stage in everything the bundle needs. For `dev` we need the full nix
# closure (binary links via rpath into /nix/store); for `portable` the
# bundle has its own bin/lib/modules and we only need those.
COPY --from=build /nix/store                 /nix/store
COPY --from=build /out/bundle                /opt/logoscore

# Both flavors put the binary at the same path, so the entrypoint can
# point straight at it — no `/usr/local/bin` symlink needed. That matters
# because `nixos/nix:2.24.9` (alpine-based) doesn't ship `/usr/local/bin/`,
# so `ln -s … /usr/local/bin/logoscore` fails at build time with
# "No such file or directory" on the parent dir.
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
