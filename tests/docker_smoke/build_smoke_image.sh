#!/usr/bin/env bash
# Builds `logoscore:smoke-<flavor>` via `docker buildx build`. Two flavors:
#
#   ./build_smoke_image.sh                  # tag: logoscore:smoke-portable (default)
#   FLAVOR=dev      ./build_smoke_image.sh  # tag: logoscore:smoke-dev
#   FLAVOR=both     ./build_smoke_image.sh  # builds both tags
#
# - `portable` (default) → .#dockerBundlePortable (cli-bundle-dir,
#                          self-contained; matches how released binaries
#                          are distributed)
# - `dev`                → .#dockerBundle         (.#cli linked against
#                          /nix/store)
#
# Note: the image is a *CLI-only runtime* — it does not bake in any user
# modules. User modules (e.g. test_basic_module for the smoke suite)
# are bind-mounted into the container at runtime; see the README and
# `LogoscoreDockerDaemon` (in `src/logoscore/docker_daemon.py`), which
# is what the smoke tests use to start the container.
#
# Everything happens inside a Linux container (stage 1 runs `nix build`
# inside nixos/nix), so macOS hosts run fine under Docker Desktop's
# native Linux VM — no nix linux-builder or cross-compile setup
# required. Same pattern as status-go's tests-functional image.
#
# The image is pinned to whatever logos-logoscore-cli revision this
# repo's flake points at. To iterate on unpublished CLI changes, push
# them to a branch and bump `logos-logoscore-cli.url` in flake.nix.
#
# ── BuildKit layer caching ─────────────────────────────────────────────
#
# When the script is invoked from CI we want the GHA buildx backend to
# cache each Dockerfile layer — most importantly the `RUN nix build` in
# the build stage, which costs ~5 min cold and is invariant whenever
# flake.nix / flake.lock / src haven't changed. Two env vars drive it:
#
#   BUILDX_CACHE_FROM   — pulled by buildx (`--cache-from`)
#   BUILDX_CACHE_TO     — pushed by buildx (`--cache-to`)
#
# Typical CI use:
#
#   BUILDX_CACHE_FROM=type=gha
#   BUILDX_CACHE_TO=type=gha,mode=max
#
# (`mode=max` exports cache for every intermediate layer, not just the
# final one — needed so the next CI run can short-circuit the build
# stage. `mode=min` would only cache the runtime stage.)
#
# Locally the env vars are unset and the script falls back to whatever
# default cache the host's docker daemon already keeps.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py_repo="$(cd "$here/../.." && pwd)"

# Default to the host's native Linux platform — avoids Rosetta
# emulation on Apple Silicon, which has been seen to trip Boost.Asio's
# socket acceptor. Override via $PLATFORM if you need a specific target.
if [[ -n "${PLATFORM:-}" ]]; then
    platform_flags=( --platform "$PLATFORM" )
else
    platform_flags=()
fi

# Only forward the cache flags when set — leaving them empty would
# pass empty strings to buildx and confuse it.
cache_flags=()
if [[ -n "${BUILDX_CACHE_FROM:-}" ]]; then
    cache_flags+=( --cache-from "$BUILDX_CACHE_FROM" )
fi
if [[ -n "${BUILDX_CACHE_TO:-}" ]]; then
    cache_flags+=( --cache-to "$BUILDX_CACHE_TO" )
fi

build_flavor() {
    local flavor="$1"
    local tag="logoscore:smoke-$flavor"
    echo "=================================================================="
    echo "Building $tag from $py_repo (FLAVOR=$flavor)"
    echo "(first run takes ~5 min while nix populates its store;"
    echo " subsequent builds are incremental)"
    echo "=================================================================="
    # `--load` exports the built image to the local docker daemon's
    # image store. Without it, buildx leaves the image inside the
    # buildx builder instance's containerd-style store, where
    # `docker run logoscore:smoke-portable` (the smoke tests' next
    # step) can't find it.
    docker buildx build \
        "${platform_flags[@]+"${platform_flags[@]}"}" \
        --build-arg "FLAVOR=$flavor" \
        -f "$here/Dockerfile" \
        -t "$tag" \
        --load \
        "${cache_flags[@]+"${cache_flags[@]}"}" \
        "$py_repo"
}

case "${FLAVOR:-portable}" in
    dev|portable)
        build_flavor "${FLAVOR:-portable}"
        ;;
    both)
        build_flavor portable
        build_flavor dev
        ;;
    *)
        echo "Unknown FLAVOR='$FLAVOR'; use dev|portable|both" >&2
        exit 1
        ;;
esac

docker images 'logoscore:smoke-*'
