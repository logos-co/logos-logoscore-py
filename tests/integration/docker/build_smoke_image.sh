#!/usr/bin/env bash
# Builds `logoscore:smoke-<flavor>` via `docker build`. Two flavors:
#
#   ./build_smoke_image.sh                  # tag: logoscore:smoke-dev
#   FLAVOR=portable ./build_smoke_image.sh  # tag: logoscore:smoke-portable
#   FLAVOR=both     ./build_smoke_image.sh  # builds both tags
#
# - `dev`      → .#dockerBundle         (.#cli + .install test modules)
# - `portable` → .#dockerBundlePortable (.#cli-bundle-dir + .install-portable)
#
# Everything happens inside a Linux container (stage 1 runs `nix build`
# inside nixos/nix), so macOS hosts run fine under Docker Desktop's
# native Linux VM — no nix linux-builder or cross-compile setup
# required. Same pattern as status-go's tests-functional image.
#
# The image is pinned to whatever logos-logoscore-cli revision this
# repo's flake points at. To iterate on unpublished CLI changes, push
# them to a branch and bump `logos-logoscore-cli.url` in flake.nix.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py_repo="$(cd "$here/../../.." && pwd)"

# Default to the host's native Linux platform — avoids Rosetta
# emulation on Apple Silicon, which has been seen to trip Boost.Asio's
# socket acceptor. Override via $PLATFORM if you need a specific target.
if [[ -n "${PLATFORM:-}" ]]; then
    platform_flags=( --platform "$PLATFORM" )
else
    platform_flags=()
fi

build_flavor() {
    local flavor="$1"
    local tag="logoscore:smoke-$flavor"
    echo "=================================================================="
    echo "Building $tag from $py_repo (FLAVOR=$flavor)"
    echo "(first run takes ~5 min while nix populates its store;"
    echo " subsequent builds are incremental)"
    echo "=================================================================="
    docker build \
        "${platform_flags[@]+"${platform_flags[@]}"}" \
        --build-arg "FLAVOR=$flavor" \
        -f "$here/Dockerfile" \
        -t "$tag" \
        "$py_repo"
}

case "${FLAVOR:-dev}" in
    dev|portable)
        build_flavor "${FLAVOR:-dev}"
        ;;
    both)
        build_flavor dev
        build_flavor portable
        ;;
    *)
        echo "Unknown FLAVOR='$FLAVOR'; use dev|portable|both" >&2
        exit 1
        ;;
esac

docker images 'logoscore:smoke-*'
