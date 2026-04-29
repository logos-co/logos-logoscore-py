#!/usr/bin/env bash
# Build one or more Logos module flakes inside docker, so the resulting
# `.so` files are Linux-binary-compatible with the smoke daemon image
# regardless of the host OS / arch.
#
# Why: the test_modules from the workspace nix flake are built for the host
# (e.g. aarch64-darwin → .dylib on macOS). The Linux container can't load
# `.dylib`s, so the daemon scans /user-modules, finds nothing it can use,
# and fails any load-module call. Same problem applies to anyone authoring
# their own module who wants to drive logoscore from a different host OS
# than the daemon runs on.
#
# This script runs `nix build` inside the same nixos/nix base the smoke
# image's stage-1 builder uses, so the resulting binaries match the
# daemon's expected glibc / Qt / OpenSSL versions byte-for-byte. All
# requested builds share one container run / one nix store, so common
# deps (logos-cpp-sdk, Qt, boost, openssl) get fetched once.
#
# Usage:
#   build_modules_in_docker.sh <output-dir> <flake#attr> [<flake#attr> ...]
#
# Examples:
#   # Single module:
#   ./build_modules_in_docker.sh ./out/modules \
#       'github:logos-co/logos-test-modules#modules.x86_64-linux.test_basic_module.install-portable'
#
#   # Several modules in one shot — second build reuses first build's
#   # downloaded SDK/Qt/boost/openssl closure:
#   ./build_modules_in_docker.sh ./out/modules \
#       'github:logos-co/logos-test-modules#modules.x86_64-linux.test_basic_module.install-portable' \
#       'github:user/my-module#packages.x86_64-linux.install-portable'
#
# Each <attr> must point at a derivation whose output contains a `modules/`
# subdirectory in the layout the daemon's `-m` flag expects
# (`modules/<name>/<name>_plugin.so` + `manifest.json`). The standard
# logos-module-builder `.install-portable` output produces exactly this.

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <output-dir> <flake#attr> [<flake#attr> ...]" >&2
    exit 1
fi

OUTPUT_DIR="$1"
shift
# Build a tab-separated "flake\tattr\nflake\tattr\n..." string for the
# in-container loop to read. Splitting on '#' from the right (rather than
# the first '#') would be more robust if attr paths could contain '#' —
# they don't, but using the leftmost split is safer and that's what
# `${spec%%#*}` does.
BUILDS_ENV=""
for spec in "$@"; do
    case "$spec" in
        *'#'*) ;;
        *) echo "Error: '$spec' is not in flake#attr form" >&2; exit 1 ;;
    esac
    flake="${spec%%#*}"
    attr="${spec#*#}"
    BUILDS_ENV+="${flake}\t${attr}\n"
done

# Pin to the same nixos/nix the smoke image's builder stage uses, so the
# build closure (glibc, Qt, openssl, boost) lines up with what the daemon
# was compiled against. If you bump this here, bump it in Dockerfile too.
BUILDER_IMAGE="${LOGOSCORE_BUILDER_IMAGE:-nixos/nix:2.24.9}"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR_ABS="$(cd "$OUTPUT_DIR" && pwd)"

echo "Building $# module(s) via $BUILDER_IMAGE ..."
echo "Output: $OUTPUT_DIR_ABS"

# Sandbox + filter-syscalls disabled because Docker Desktop's seccomp
# profile + Rosetta layer can block them in nested-container builds.
# The outer docker layer is already isolating the build.
docker run --rm \
    -v "$OUTPUT_DIR_ABS:/out" \
    -e BUILDS="$BUILDS_ENV" \
    "$BUILDER_IMAGE" \
    sh -c '
        set -e
        mkdir -p /etc/nix
        {
            echo "experimental-features = nix-command flakes"
            echo "sandbox = false"
            echo "filter-syscalls = false"
        } > /etc/nix/nix.conf

        # Each build gets its own /tmp/result-N out-link; merging into
        # /out happens per-build so a partial run still leaves usable
        # output. Common deps fetched by build #0 (Qt / openssl /
        # boost / logos-cpp-sdk) are reused by builds #1+ from the
        # shared in-container nix store.
        i=0
        printf "%b" "$BUILDS" | while IFS="$(printf \\t)" read -r flake attr; do
            [ -n "$flake" ] || continue
            echo "[$i] building $flake#$attr"
            nix build -L "$flake#$attr" --out-link "/tmp/result-$i" --refresh

            if [ ! -d "/tmp/result-$i/modules" ]; then
                echo "ERROR: $flake#$attr has no modules/ subdir" >&2
                ls -la "/tmp/result-$i/" >&2
                exit 1
            fi

            # find + install -m 644: writes every regular file with
            # explicit perms (644), bypassing tar/cp `--preserve` logic
            # that would otherwise inherit the read-only nix-store perms
            # on the source. Docker Desktop bind mounts on macOS reject
            # `chmod` from inside the container, so we can't un-readonly
            # after the fact — the perms have to be right at write time.
            cd "/tmp/result-$i/modules"
            find . -type d | while read -r d; do mkdir -p "/out/$d"; done
            find . -type f | while read -r f; do install -m 644 "$f" "/out/$f"; done
            cd -
            i=$((i+1))
        done
    '

echo "Done. Modules written to $OUTPUT_DIR_ABS"
ls -la "$OUTPUT_DIR_ABS"
