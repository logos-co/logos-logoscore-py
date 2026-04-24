#!/usr/bin/env bash
# Builds `logoscore:smoke-<flavor>` via `docker build`. Two flavors:
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
# `test_docker_smoke.py::_run_daemon_container`.
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
py_repo="$(cd "$here/../.." && pwd)"

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
