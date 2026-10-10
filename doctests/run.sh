#!/usr/bin/env bash
#
# Execute every logos-logoscore-py doc-test end-to-end and regenerate its Markdown.
#
# The runner is the shared `doctest` CLI
# (https://github.com/logos-co/logos-doctest), invoked directly via its flake.
# For each *.test.yaml in this directory, `doctest run` executes every command
# end-to-end and asserts on the output; `doctest generate` renders the same spec
# to Markdown under outputs/ (the *.test.yaml spec is the source of truth).
#
# To run against a local logos-doctest checkout instead of the published flake,
# set DOCTEST, e.g.:  DOCTEST="nix run path:../../logos-doctest --" ./run.sh
#
set -euo pipefail

# Run from this doctests/ directory regardless of where the script is invoked from.
cd "$(dirname "$0")"

# The doctest CLI. Override by exporting DOCTEST (space-separated command).
read -r -a DOCTEST <<< "${DOCTEST:-nix run github:logos-co/logos-doctest --}"

# Build the doc-tests against THIS repo's current commit rather than the latest
# published flake. Each spec's `github:logos-co/logos-logoscore-py{release}` URL is pinned to
# $COMMIT via --release-for, so the run exercises exactly what's checked out here.
# Override by exporting COMMIT (e.g. a tag), or set COMMIT="" to fall back to latest.
#
# Note: nix fetches the commit from the GitHub remote, so $COMMIT must be pushed
# to logos-co/logos-logoscore-py. A local-only / uncommitted HEAD won't resolve; export
# COMMIT="" (or push first) in that case.
COMMIT="${COMMIT-$(git rev-parse HEAD)}"
RELEASE_FOR=()
if [ -n "${COMMIT}" ]; then
  RELEASE_FOR=(--release-for "logos-logoscore-py=${COMMIT}")
  echo "==> Pinning logos-logoscore-py to ${COMMIT}"
else
  echo "==> COMMIT empty; building logos-logoscore-py from latest"
fi

mkdir -p outputs

for spec in *.test.yaml; do
  name="$(basename "${spec%.test.yaml}")"
  echo "==> Running ${spec}"
  # ${RELEASE_FOR[@]+...} guards the expansion so an empty array doesn't trip
  # `set -u` on older bash (e.g. macOS's stock 3.2).
  "${DOCTEST[@]}" run "${spec}" --verbose --continue-on-fail \
    ${RELEASE_FOR[@]+"${RELEASE_FOR[@]}"}

  echo "==> Generating outputs/${name}.md"
  "${DOCTEST[@]}" generate "${spec}" \
    ${RELEASE_FOR[@]+"${RELEASE_FOR[@]}"} \
    -o "outputs/${name}.md"
done

echo "==> Done. Rendered docs are in doctests/outputs/"
