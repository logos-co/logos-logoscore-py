# syntax=docker/dockerfile:1.6
#
# Reusable logosctl daemon image. Contains only the CLI and the modules it
# ships with; user modules are bind-mounted in at runtime at /user-modules.
#
# Builds entirely inside a Linux container — no cross-compilation from
# the host.
#
# Usage (from this repo's root):
#   tests/logosctl/docker_smoke/build_smoke_image.sh
#
# Tag: logosctl:smoke-portable, from the flake's
# .#logosctlDockerBundlePortable.

FROM nixos/nix:2.24.9 AS build

# Enable flakes. Disable the sandbox + seccomp filter: Docker Desktop's
# Rosetta layer (on Apple Silicon's linux/amd64 fallback) blocks the
# seccomp BPF that nix's sandbox wants to install, and the docker layer
# already isolates the build.
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

RUN nix build -L .#logosctlDockerBundlePortable --out-link /out/bundle

# ubuntu:24.04 ships a normal glibc runtime, so the portable bundle's ELF
# interpreter and system libraries resolve at their canonical paths.
FROM ubuntu:24.04
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /out/bundle /opt/logosctl
EXPOSE 6000 6001
ENTRYPOINT ["/opt/logosctl/bin/logosctl"]
CMD ["daemon", "start"]
