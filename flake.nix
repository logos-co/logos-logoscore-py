{
  description = "Python wrappers for the logoscore and logosctl CLIs — launch daemons, load modules, call methods, subscribe to events";

  inputs = {
    logos-nix.url = "github:logos-co/logos-nix";
    nixpkgs.follows = "logos-nix/nixpkgs";
    # The module-transport matrix depends on the qt_remote_plain feature chain
    # and the explicit transport variants exported by logos-test-modules; its
    # in-process coordinate on the runtime-control wave (logoscore-cli#145 and
    # logos-test-modules' feat/inproc-coordinates), with legacy mode deleted on
    # top (the feat/drop-legacy-mode branches), and the daemon's runtime in a
    # process of its own (logoscore-cli's feat/runtime-process). Peering sits on
    # top (logoscore-cli#149): `logosctl peer`, which PeeredDaemons drives.
    logos-logoscore-cli.url = "github:logos-co/logos-logoscore-cli/feat/peering";
    # feat/peering: feat/runtime-process's modules plus test_concurrency_cpp.
    logos-test-modules.url = "github:logos-co/logos-test-modules/feat/peering";
    # logos-test-modules at its last commit before the qt_remote_plain chain,
    # built from its own lock: unchanged binaries for the transport matrix to
    # pair with the new runtime. No follows, on purpose: following would
    # rebuild them against this flake's protocol.
    logos-test-modules-release.url =
      "github:logos-co/logos-test-modules/5d991467d585c9c19ba1168a0bd9159b8b9b1c55";
  };

  outputs = { self, nixpkgs, logos-logoscore-cli, logos-test-modules,
              logos-test-modules-release, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f {
        inherit system;
        pkgs = import nixpkgs { inherit system; };
      });

    in
    {
      # ── Packages ──────────────────────────────────────────────────────────
      # `nix build` produces a Python wheel. The `logoscore` CLI is propagated
      # so anyone using this package also has the binary on PATH.
      #
      # `dockerBundle` / `dockerBundlePortable` (Linux only) prepare the
      # logosctl `out/bundle` directory consumed by `tests/docker_smoke/Dockerfile`
      # — the smoke image's stage-1 nix-build copies it into the
      # ubuntu-based runtime stage. The actual docker image is built
      # via `tests/docker_smoke/build_smoke_image.sh`, not directly
      # from these flake outputs.
      packages = forAllSystems ({ pkgs, system }:
        let
          logoscoreBin = logos-logoscore-cli.packages.${system}.default;
          pythonPkg = pkgs.python3Packages.buildPythonPackage {
            pname = "logoscore";
            version = "0.1.0";
            format = "pyproject";
            src = ./.;
            nativeBuildInputs = [ pkgs.python3Packages.hatchling ];
            # Only `logoscore` is propagated, even though the wheel now also
            # ships the `logosctl` client. Propagating `ctl` would put both
            # binaries on the critical path of `nix build` — the one output
            # every consumer of this flake pulls. Anyone who wants logosctl
            # takes it from `logos-logoscore-cli.packages.*.ctl` explicitly
            # (the dev shell and the logosctl checks below do exactly that).
            propagatedBuildInputs = [ logoscoreBin ];
            doCheck = false;
            pythonImportsCheck = [ "logoscore" "logosctl" ];
          };

          # ── Docker bundles ─────────────────────────────────────────────
          # The bundle is **just the logosctl CLI** plus the modules it
          # ships with (capability_module, modules_state, the peering
          # modules, and the package modules). No test modules — those get
          # bind-mounted at runtime (`-v $modules_dir:/user-modules`, named
          # in the daemon config's `modules_dirs`). That makes the image
          # reusable for anyone who wants to test their own module against
          # a daemon they operate over Remote Runtime Control.
          #
          # Two flavors:
          #
          #   * `dockerBundle` (dev) — the `ctl` package: logosctl, its
          #     runtime and module hosts, linked against Qt/Boost/OpenSSL
          #     in the nix store via rpath, so the runtime image MUST ship
          #     the nix store. Its modules/ sits beside bin/, where the
          #     daemon finds it.
          #
          #   * `dockerBundlePortable` — `ctl-bundle-dir`, a self-contained
          #     `bin/ + lib/ + modules/` tree with every Qt dep copied in.
          #     Larger but runs standalone — no nix store needed.
          #
          # The Dockerfile picks one via `--build-arg FLAVOR=dev|portable`.
          # The pytest suite parametrises over both flavors so regressions
          # in either path surface in the smoke matrix.

          logosctlBin = logos-logoscore-cli.packages.${system}.ctl;
          logosctlPortable = logos-logoscore-cli.packages.${system}.ctl-bundle-dir;

          dockerBundle = pkgs.runCommand "logosctl-bundle-dev" { } ''
            # Dev flavor: the ctl tree, dereferenced. Its rpaths point into
            # /nix/store (copied wholesale in Dockerfile stage 2).
            mkdir -p $out
            cp -rL ${logosctlBin}/. $out/
            chmod -R u+w $out
          '';

          dockerBundlePortable = pkgs.runCommand "logosctl-bundle-portable" { } ''
            # Portable flavor: ctl-bundle-dir is already a self-contained
            # bin/ + lib/ + modules/ tree. Copy it as-is.
            mkdir -p $out
            cp -r ${logosctlPortable}/* $out/
            chmod -R u+w $out
          '';
        in {
          default              = pythonPkg;
          logoscore-py         = pythonPkg;
          dockerBundle         = dockerBundle;
          dockerBundlePortable = dockerBundlePortable;
        }
      ) // {
        # What .github/workflows/windows.yml stages for the logosctl suite:
        # logosctl.exe, and test_fullapi_cpp installed as a portable module.
        x86_64-windows = {
          ctl = logos-logoscore-cli.packages.x86_64-windows.ctl;
          test-modules = (logos-test-modules.inputs.logos-module-builder.lib.mkLogosModule {
            src = "${logos-test-modules}/test-fullapi-module-cpp";
            configFile = "${logos-test-modules}/test-fullapi-module-cpp/metadata.json";
          }).packages.x86_64-windows.install-portable;
        };
      };

      # ── Dev shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into a shell with python + pytest + logoscore
      # + a pre-built test_fullapi_cpp install tree, so `pytest` just works
      # without any extra environment setup. The nix `integration` check
      # sets the same two env vars, so the dev shell matches CI behaviour.
      devShells = forAllSystems ({ pkgs, system }:
        let
          logoscoreBin             = logos-logoscore-cli.packages.${system}.default;
          # Both binaries are on PATH here so a plain `pytest` covers both
          # suites — tests/logosctl skips silently when LOGOSCTL_BIN is unset,
          # which would make the new suite look green while never running.
          logosctlBin              = logos-logoscore-cli.packages.${system}.ctl;
          # `test_fullapi_cpp` (universal C++) is the single test module the
          # suite loads — its methods + typed events span the whole
          # parameter/return/event surface. `.install` lays out
          # modules/<name>/… ready for the daemon's `-m` flag.
          testModulesInstall         = logos-test-modules.modules.${system}.test_fullapi_cpp.install;
          # Self-contained, so it loads in either docker smoke image.
          testModulesInstallPortable = logos-test-modules.modules.${system}.test_fullapi_cpp.install-portable;
          # Its plain build, the one a daemon can export (test_peering.py).
          testModulesPlainInstall =
            logos-test-modules.modules.${system}.test_fullapi_cpp_qt_remote_plain.install;
        in {
        default = pkgs.mkShell ({
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ]))
            logoscoreBin
            logosctlBin
          ];

          # Integration tests skip when these are unset (by design, so
          # `pytest` on a plain Python env doesn't try to spawn daemons).
          # Exporting them here means the dev shell exercises the full
          # suite out of the box.
          LOGOSCORE_BIN                       = "${logoscoreBin}/bin/logoscore";
          LOGOSCORE_TEST_MODULES_DIR          = "${testModulesInstall}/modules";

          # The logosctl suite reads its own pair of variables (its conftest
          # rebinds `test_modules_dir` to LOGOSCTL_TEST_MODULES_DIR) so a
          # machine can point the two suites at different builds. Here they
          # are the same modules — the module ABI is shared, only the CLI differs.
          LOGOSCTL_BIN                        = "${logosctlBin}/bin/logosctl";
          LOGOSCTL_TEST_MODULES_DIR           = "${testModulesInstall}/modules";
          LOGOSCTL_PLAIN_MODULES_DIR          = "${testModulesPlainInstall}/modules";

          shellHook = ''
            echo "logos-logoscore-py dev shell"
            echo "  python:                                  $(python --version)"
            echo "  logoscore:                               $(logoscore --version 2>/dev/null || echo 'not on PATH')"
            echo "  logosctl:                                $(logosctl --version 2>/dev/null || echo 'not on PATH')"
            echo "  LOGOSCORE_BIN:                           $LOGOSCORE_BIN"
            echo "  LOGOSCORE_TEST_MODULES_DIR:              $LOGOSCORE_TEST_MODULES_DIR"
            echo "  LOGOSCTL_BIN:                            $LOGOSCTL_BIN"
            echo "  LOGOSCTL_TEST_MODULES_DIR:               $LOGOSCTL_TEST_MODULES_DIR"
            echo "  LOGOSCTL_PLAIN_MODULES_DIR:              $LOGOSCTL_PLAIN_MODULES_DIR"
            echo "  LOGOSCTL_DOCKER_MODULES_DIR:             ''${LOGOSCTL_DOCKER_MODULES_DIR:-(built in docker)}"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        } // pkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          # The docker smoke mounts these into its daemons; elsewhere a host
          # build would not load in the Linux container, so it builds them
          # in docker.
          LOGOSCTL_DOCKER_MODULES_DIR         = "${testModulesInstallPortable}/modules";
        });
      });

      # ── Checks ────────────────────────────────────────────────────────────
      # `nix flake check` runs the unit tests (no daemon required) and the
      # integration test suite against a real CLI + test modules.
      #
      # Both suites exist twice, once per client: `unit` / `integration-local`
      # drive `logoscore`, `unit-logosctl` / `integration-logosctl-local`
      # drive `logosctl`. Separate derivations throughout, never one
      # derivation looping over both — see the `unit-logosctl` comment.
      checks = forAllSystems ({ pkgs, system }:
        let
          python = pkgs.python3.withPackages (ps: [ ps.pytest ]);
          logoscoreBin = logos-logoscore-cli.packages.${system}.default;
          # Same repo, sibling output: `ctl` ships alongside `default`/
          # `cli` since logos-logoscore-cli#76. Referenced only from the
          # `*-logosctl` checks below so a logosctl hiccup cannot redden a
          # logoscore check's evaluation path either.
          logosctlBin = logos-logoscore-cli.packages.${system}.ctl;
          # `.install` lays out modules/<name>/<name>_plugin.{so,dylib} +
          # manifest.json — the layout logoscore's `-m` flag expects.
          # `test_fullapi_cpp` (universal C++) is the single test module the
          # integration suite loads; its methods + typed events span the
          # whole parameter/return/event surface.
          testModulesInstall = logos-test-modules.modules.${system}.test_fullapi_cpp.install;
          # The conformance matrix replays every case against BOTH providers —
          # a divergence between them is a finding in its own right, and one
          # provider must never be able to satisfy an assertion for the other.
          testModulesRustInstall = logos-test-modules.modules.${system}.test_fullapi_rust.install;
          # The ext contract (records, bytes at depth, typed maps, nested
          # composites). The C++ cdylib backend could not express these types
          # when the table was split out, so it ran single-provider and without a
          # differential; logos-cpp-sdk#125 lifted that and both providers are
          # wired below, so this table carries a provider differential like
          # full_api. Its consumer axis is `testModulesExtQtProxyInstall` below.
          testModulesExtInstall = logos-test-modules.modules.${system}.test_fullapi_ext_rust.install;
          testModulesExtCppInstall = logos-test-modules.modules.${system}.test_fullapi_ext_cpp.install;
          # The QT-TYPED consumer. `type: core` with no `interface` key selects
          # apiStyle=qt, so this module's generated wrappers are the Qt ones —
          # the surface the two existing proxies cannot reach (universal forces
          # apiStyle=lp, cdylib forces the Rust client). It forwards the whole
          # contract, so the case table replays through it unchanged, twice:
          # once per generated wrapper table (sync / async).
          testModulesQtProxyInstall =
            logos-test-modules.modules.${system}.test_fullapi_qtproxy.install;
          # The ext table's Qt-typed consumer, and the reason it is a SECOND
          # module rather than a second binding of the first: a Qt consumer
          # wrapper is generated per CONTRACT, and full_api_ext is a different
          # contract. It is also where the widened Qt mapping actually lives —
          # records, QList<Blob>, QMap<QString, QList<QByteArray>>,
          # QList<QList<qlonglong>>, std::optional<QString> — none of which
          # full_api can spell, so none of which qtproxy-sync/async execute.
          testModulesExtQtProxyInstall =
            logos-test-modules.modules.${system}.test_fullapi_ext_qtproxy.install;

          # Explicit module-process transport builds: they choose how each
          # module host talks to logoscore. Provider and proxy builds are paired
          # independently below so mixed QRO/plain topologies cannot hide.
          transportCppQro =
            logos-test-modules.modules.${system}.test_fullapi_cpp_qt_remote.install;
          transportRustQro =
            logos-test-modules.modules.${system}.test_fullapi_rust_qt_remote.install;
          transportProxyQro =
            logos-test-modules.modules.${system}.test_fullapi_proxy_qt_remote.install;
          transportCppPlain =
            logos-test-modules.modules.${system}.test_fullapi_cpp_qt_remote_plain.install;
          transportRustPlain =
            logos-test-modules.modules.${system}.test_fullapi_rust_qt_remote_plain.install;
          transportProxyPlain =
            logos-test-modules.modules.${system}.test_fullapi_proxy_qt_remote_plain.install;
          transportExtCppPlain =
            logos-test-modules.modules.${system}.test_fullapi_ext_cpp_qt_remote_plain.install;
          transportExtRustPlain =
            logos-test-modules.modules.${system}.test_fullapi_ext_rust_qt_remote_plain.install;
          # A plain provider declared `concurrency: multi` (test_peering.py).
          concurrencyPlain =
            logos-test-modules.modules.${system}.test_concurrency_cpp.install;

          # The released modules, and the other consumers every coordinate runs:
          # the Rust LP proxy and the generated Qt glue (qt_remote only), so a
          # typed Qt consumer meets a plain provider in some coordinate.
          releasedCpp = logos-test-modules-release.modules.${system}.test_fullapi_cpp.install;
          releasedRust = logos-test-modules-release.modules.${system}.test_fullapi_rust.install;
          releasedProxy = logos-test-modules-release.modules.${system}.test_fullapi_proxy.install;
          # The daemon those modules were released with, from that input's own
          # lock, with its own host: it can only host qt_remote modules.
          releasedLogoscore =
            logos-test-modules-release.inputs.logos-logoscore-cli.packages.${system}.default;
          testModulesProxyRustInstall =
            logos-test-modules.modules.${system}.test_fullapi_proxy_rust.install;

          # Helper: run the integration suite. Same env wiring as the unit
          # check, plus the CLI and the test modules.
          mkIntegration = pkgs.runCommand
            "logoscore-py-integration-tests" {
              nativeBuildInputs = [ python logoscoreBin ]
                ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.qt6.qtbase ];
            } ''
              cp -r ${./.}/. .
              chmod -R +w .
              export QT_QPA_PLATFORM=offscreen
              export QT_FORCE_STDERR_LOGGING=1
              ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
                export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
              ''}
              export PYTHONPATH=$PWD/src
              export LOGOSCORE_BIN=${logoscoreBin}/bin/logoscore
              export LOGOSCORE_TEST_MODULES_DIR=${testModulesInstall}/modules
              # Run from a writable HOME so any stray ~/.logoscore writes are isolated.
              export HOME=$PWD/home
              mkdir -p $HOME
              ${python}/bin/pytest tests/integration -v
              touch $out
            '';

          # Helper: the same thing for the logosctl suite. A sibling rather
          # than a `binary:`/`suite:` parameter on `mkIntegration`, for the
          # reason the two test trees are duplicated in the first place — the
          # two CLIs configure a daemon through different mechanisms, and
          # retiring logoscore should be a delete, not an untangle. The two
          # helpers drifting apart is expected, not a smell.
          mkIntegrationLogosctl = pkgs.runCommand
            "logosctl-py-integration-tests" {
              nativeBuildInputs = [ python logosctlBin ]
                ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.qt6.qtbase ];
            } ''
              cp -r ${./.}/. .
              chmod -R +w .
              export QT_QPA_PLATFORM=offscreen
              export QT_FORCE_STDERR_LOGGING=1
              ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
                export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
              ''}
              export PYTHONPATH=$PWD/src
              export LOGOSCTL_BIN=${logosctlBin}/bin/logosctl
              export LOGOSCTL_TEST_MODULES_DIR=${testModulesInstall}/modules
              # test_peering.py exports test_fullapi_cpp, which takes its plain build,
              # and test_concurrency_cpp.
              export LOGOSCTL_PLAIN_MODULES_DIR=${transportCppPlain}/modules
              export LOGOSCTL_CONCURRENCY_MODULES_DIR=${concurrencyPlain}/modules
              # tests/logosctl/conftest.py SKIPS when either of those is unset,
              # so a rename here turns the whole suite green-by-omission.
              #
              # Writable HOME: logosctl defaults to ~/.logosctl and refuses to
              # start a second daemon in a config dir that already holds a live
              # one. Every test drives an isolated --config-dir, but a stray
              # default-session write must still land somewhere sandbox-local.
              export HOME=$PWD/home
              mkdir -p $HOME
              ${python}/bin/pytest tests/logosctl/integration -v
              touch $out
            '';

          # Run the complete full_api contract through one module-transport
          # coordinate. `py` measures both providers directly and `lp-proxy`
          # adds the independently selected forwarding module, so each result
          # covers provider <-> logoscore and proxy <-> logoscore as well as the
          # proxy -> provider call.
          mkModuleTransportMatrixArgs = daemon: extraArgs: label: cppInstall: rustInstall: proxyInstall:
            pkgs.runCommand "logoscore-py-module-transport-${label}" {
              nativeBuildInputs = [ python daemon ]
                ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.qt6.qtbase ];
            } ''
              cp -r ${./.}/. .
              chmod -R +w .
              export QT_QPA_PLATFORM=offscreen
              export QT_FORCE_STDERR_LOGGING=1
              ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
                export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
              ''}
              export PYTHONPATH=$PWD/src
              export HOME=$PWD/home
              mkdir -p $HOME $out
              ${python}/bin/python conformance/run_matrix.py \
                --logoscore ${daemon}/bin/logoscore \
                --cases ${logos-test-modules}/conformance/cases.json \
                --known ${logos-test-modules}/conformance/known.json \
                --contract ${logos-test-modules}/test-fullapi-proxy-module-rust/full_api.lidl \
                --cpp-modules ${cppInstall}/modules \
                --rust-modules ${rustInstall}/modules \
                --proxy-consumer lp-proxy=test_fullapi_proxy=${proxyInstall}/modules \
                --proxy-consumer rust-proxy=test_fullapi_proxy_rust=${testModulesProxyRustInstall}/modules \
                --proxy-consumer qtproxy-sync=test_fullapi_qtproxy=${testModulesQtProxyInstall}/modules=sync \
                --proxy-consumer qtproxy-async=test_fullapi_qtproxy=${testModulesQtProxyInstall}/modules=async \
                ${extraArgs} \
                --jsonl $out/matrix.jsonl \
                --report $out/matrix.html \
                --no-color \
                2>&1 | tee $out/matrix.txt
            '';
          mkModuleTransportMatrixWith = daemon: mkModuleTransportMatrixArgs daemon "";
          mkModuleTransportMatrix = mkModuleTransportMatrixWith logoscoreBin;

          # The ext table for a given pair of providers; its consumers are py
          # and the ext Qt proxy (see conformance-matrix-ext).
          mkExtMatrix = name: rustInstall: cppInstall: pkgs.runCommand name {
            nativeBuildInputs = [ python logoscoreBin ]
              ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.qt6.qtbase ];
          } ''
            cp -r ${./.}/. .
            chmod -R +w .
            export QT_QPA_PLATFORM=offscreen
            export QT_FORCE_STDERR_LOGGING=1
            ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
              export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
            ''}
            export PYTHONPATH=$PWD/src
            export HOME=$PWD/home
            mkdir -p $HOME $out
            ${python}/bin/python conformance/run_matrix.py \
              --logoscore ${logoscoreBin}/bin/logoscore \
              --cases   ${logos-test-modules}/conformance/ext-cases.json \
              --known   ${logos-test-modules}/conformance/known-ext.json \
              --contract ${logos-test-modules}/test-fullapi-ext-module-rust/rust-lib/test_fullapi_ext_rust.lidl \
              --modules test_fullapi_ext_rust=${rustInstall}/modules \
              --modules test_fullapi_ext_cpp=${cppInstall}/modules \
              --proxy-consumer 'extqtproxy-sync=test_fullapi_ext_qtproxy=${testModulesExtQtProxyInstall}/modules=sync=echoStringMap:[{"k":"v"}]' \
              --proxy-consumer 'extqtproxy-async=test_fullapi_ext_qtproxy=${testModulesExtQtProxyInstall}/modules=async=echoStringMap:[{"k":"v"}]' \
              --jsonl $out/matrix-ext.jsonl \
              --report $out/matrix-ext.html \
              --no-color \
              2>&1 | tee $out/matrix-ext.txt
          '';
        in
        # `rec` so `conformance-matrix-merged` can name the two runs it is built
        # from. It depends on them; it does not re-measure anything.
        rec {
          unit = pkgs.runCommand "logoscore-py-unit-tests" {
            nativeBuildInputs = [ python ];
          } ''
            cp -r ${./.}/. .
            chmod -R +w .
            export PYTHONPATH=$PWD/src
            # The inline-vs-shared table guard resolves the shared table by
            # sibling checkout, which does not exist in the sandbox — so without
            # this it SKIPPED here and ran only on a developer's workspace. A
            # drift guard that is green because it never ran is worse than no
            # guard: pointed at the table it found two boundaries pinned inline
            # and absent from cases.json.
            export LOGOS_CONFORMANCE_DIR=${logos-test-modules}/conformance
            ${python}/bin/pytest tests/unit -v
            touch $out
          '';

          # ── logosctl ────────────────────────────────────────────────────
          # A parallel suite for the parallel client, in its own derivations
          # so nix builds it concurrently with the logoscore ones and a red
          # logosctl cannot mask a logoscore regression. Dropping logosctl
          # later is deleting `unit-logosctl`, `integration-logosctl-local`,
          # `mkIntegrationLogosctl`, and `logosctlBin`.
          #
          # Deliberately NOT duplicated: the conformance matrix. It measures
          # the LIDL type contract, which lives in the runtime both binaries
          # embed — replaying the whole matrix through a second CLI would
          # double the longest job in the flake for no signal the first run
          # does not already carry.
          unit-logosctl = pkgs.runCommand "logosctl-py-unit-tests" {
            nativeBuildInputs = [ python ];
          } ''
            cp -r ${./.}/. .
            chmod -R +w .
            export PYTHONPATH=$PWD/src
            # No LOGOSCTL_BIN: these tests drive a fake binary and assert on
            # argv, env and the generated config documents. Nothing here
            # spawns a daemon, which is why they run without the CLI closure.
            ${python}/bin/pytest tests/logosctl/unit -v
            touch $out
          '';

          # The LIDL conformance matrix: every (type x position) in the
          # `full_api` contract, replayed against BOTH providers and every
          # CONSUMER surface, reported as per-cell coordinates. The case table
          # and the xfail registry live in logos-test-modules/conformance/ (with
          # the providers they describe); this repo owns the driver because it
          # owns the client it uses.
          #
          # Three consumers, one run — they have to share a process for the
          # consumer differential to exist at all:
          #   py             this package's client, talking to the provider
          #   qtproxy-sync   Qt-typed wrappers, sync table  (_result.toT())
          #   qtproxy-async  Qt-typed wrappers, async table (qvariant_cast<T>)
          #
          # Fails on: a red cell, an `xpass` (a registered known-broken cell
          # that started passing — the registry has to be updated), a `skip`
          # entry whose cell turns out to work, or a (type, position) the
          # contract declares and no case covers.
          #
          # Three artifacts land in $out, and none of them is the verdict — the
          # exit status is: matrix.txt (the terminal report, with the TYPE x
          # POSITION grid and the differential), matrix.jsonl (one object per
          # cell, unchanged), and matrix.html — a self-contained page with no
          # external requests, publishable to Pages the way the doctest
          # harness's report already is.
          conformance-matrix = pkgs.runCommand "logoscore-py-conformance-matrix" {
            nativeBuildInputs = [ python logoscoreBin ]
              ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.qt6.qtbase ];
          } ''
            cp -r ${./.}/. .
            chmod -R +w .
            export QT_QPA_PLATFORM=offscreen
            export QT_FORCE_STDERR_LOGGING=1
            ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
              export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
            ''}
            export PYTHONPATH=$PWD/src
            export HOME=$PWD/home
            mkdir -p $HOME $out
            ${python}/bin/python conformance/run_matrix.py \
              --logoscore ${logoscoreBin}/bin/logoscore \
              --cases   ${logos-test-modules}/conformance/cases.json \
              --known   ${logos-test-modules}/conformance/known.json \
              --contract ${logos-test-modules}/test-fullapi-proxy-module-rust/full_api.lidl \
              --cpp-modules  ${testModulesInstall}/modules \
              --rust-modules ${testModulesRustInstall}/modules \
              --proxy-consumer qtproxy-sync=test_fullapi_qtproxy=${testModulesQtProxyInstall}/modules=sync \
              --proxy-consumer qtproxy-async=test_fullapi_qtproxy=${testModulesQtProxyInstall}/modules=async \
              --jsonl $out/matrix.jsonl \
              --report $out/matrix.html \
              --md $out/known-broken.md \
              --no-color \
              2>&1 | tee $out/matrix.txt
          '';

          # The ext contract, with the SAME three-consumer shape the full_api
          # gate has:
          #   py                 this package's client, talking to the provider
          #   extqtproxy-sync    Qt-typed wrappers, sync table
          #   extqtproxy-async   Qt-typed wrappers, async table
          #
          # The proxy is a DIFFERENT module from the full_api one because a Qt
          # consumer wrapper is generated per contract. The probe method is
          # given explicitly (fifth field of --proxy-consumer): the driver's
          # default is `echoInt`, which full_api_ext does not have.
          conformance-matrix-ext = mkExtMatrix "logoscore-py-conformance-matrix-ext"
            testModulesExtInstall testModulesExtCppInstall;

          # Module-process transport matrix. The first name is the provider
          # transport and the second is the forwarding LP proxy transport.
          conformance-transport-qro-qro = mkModuleTransportMatrix
            "provider-qro-proxy-qro"
            transportCppQro transportRustQro transportProxyQro;
          conformance-transport-qro-plain = mkModuleTransportMatrix
            "provider-qro-proxy-plain"
            transportCppQro transportRustQro transportProxyPlain;
          conformance-transport-plain-qro = mkModuleTransportMatrix
            "provider-plain-proxy-qro"
            transportCppPlain transportRustPlain transportProxyQro;
          conformance-transport-plain-plain = mkModuleTransportMatrix
            "provider-plain-proxy-plain"
            transportCppPlain transportRustPlain transportProxyPlain;
          # Unchanged released binaries against the new runtime, both ways.
          conformance-transport-released-plain = mkModuleTransportMatrix
            "provider-released-proxy-plain"
            releasedCpp releasedRust transportProxyPlain;
          conformance-transport-plain-released = mkModuleTransportMatrix
            "provider-plain-proxy-released"
            transportCppPlain transportRustPlain releasedProxy;
          # This chain's modules under the released daemon and host, over the
          # one transport those have.
          conformance-transport-released-daemon = mkModuleTransportMatrixWith releasedLogoscore
            "daemon-released-provider-qro-proxy-qro"
            transportCppQro transportRustQro transportProxyQro;
          # The plain providers hosted in the daemon's own process: their
          # directories count as bundled and the runtime places modules in-process
          # when their build allows it, which the run then checks it did.
          conformance-transport-inproc = mkModuleTransportMatrixArgs logoscoreBin
            (pkgs.lib.escapeShellArgs [
              "--daemon-arg=--bundled-modules-dir" "--daemon-arg=${transportCppPlain}/modules"
              "--daemon-arg=--bundled-modules-dir" "--daemon-arg=${transportRustPlain}/modules"
              "--daemon-arg=--placement" ''--daemon-arg={"default":"inproc"}''
              "--expect-placement" "test_fullapi_cpp=inproc"
              "--expect-placement" "test_fullapi_rust=inproc"
            ])
            "provider-inproc-proxy-plain"
            transportCppPlain transportRustPlain transportProxyPlain;
          # The ext table with both providers over the plain transport.
          conformance-transport-ext-plain = mkExtMatrix "logoscore-py-module-transport-ext-plain"
            transportExtRustPlain transportExtCppPlain;
          # Each plain provider again through an import (<provider>@peered):
          # exported by one logosctl daemon, called on another through its
          # facade, and compared cell by cell with the provider measured here.
          conformance-transport-peered = pkgs.runCommand "logoscore-py-module-transport-peered" {
            nativeBuildInputs = [ python logoscoreBin logosctlBin ]
              ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.qt6.qtbase ];
          } ''
            cp -r ${./.}/. .
            chmod -R +w .
            export QT_QPA_PLATFORM=offscreen
            export QT_FORCE_STDERR_LOGGING=1
            ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
              export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}"
            ''}
            export PYTHONPATH=$PWD/src
            export HOME=$PWD/home
            mkdir -p $HOME $out
            ${python}/bin/python conformance/run_matrix.py \
              --logoscore ${logoscoreBin}/bin/logoscore \
              --logosctl ${logosctlBin}/bin/logosctl \
              --peered \
              --cases ${logos-test-modules}/conformance/cases.json \
              --known ${logos-test-modules}/conformance/known.json \
              --contract ${logos-test-modules}/test-fullapi-proxy-module-rust/full_api.lidl \
              --cpp-modules ${transportCppPlain}/modules \
              --rust-modules ${transportRustPlain}/modules \
              --jsonl $out/matrix.jsonl \
              --report $out/matrix.html \
              --no-color \
              2>&1 | tee $out/matrix.txt
          '';

          # The released coordinates are only worth their name if nothing
          # rebuilt those modules against this flake's protocol.
          released-modules-unchanged = pkgs.runCommand "logoscore-py-released-modules-unchanged" {
            nativeBuildInputs = [ pkgs.jq ];
          } ''
            current=$(${logoscoreBin}/bin/logos_host --inspect \
              "$(find -L ${transportCppQro}/modules -name '*_plugin.so' -o -name '*_plugin.dylib' | head -1)" \
              | jq -r .logos_protocol_version)
            case "$current" in ""|null) echo "no protocol stamp on the current build" >&2; exit 1 ;; esac
            released=""
            for install in ${releasedCpp} ${releasedRust} ${releasedProxy}; do
              plugin=$(find -L "$install/modules" -name '*_plugin.so' -o -name '*_plugin.dylib' | head -1)
              stamp=$(${logoscoreBin}/bin/logos_host --inspect "$plugin" | jq -r .logos_protocol_version)
              echo "$plugin: $stamp (current $current)"
              if [ -z "$stamp" ] || [ "$stamp" = null ] || [ "$stamp" = "$current" ] \
                 || { [ -n "$released" ] && [ "$stamp" != "$released" ]; }; then
                echo "not the released build: $plugin" >&2; exit 1
              fi
              released=$stamp
            done
            echo "$released" > $out
          '';

          conformance-transport-matrix =
            pkgs.runCommand "logoscore-py-module-transport-matrix" {} ''
              mkdir -p $out/qro-qro $out/qro-plain $out/plain-qro $out/plain-plain \
                       $out/released-plain $out/plain-released $out/released-daemon $out/ext-plain \
                       $out/inproc-plain $out/peered
              cp -r ${conformance-transport-qro-qro}/. $out/qro-qro/
              cp -r ${conformance-transport-qro-plain}/. $out/qro-plain/
              cp -r ${conformance-transport-plain-qro}/. $out/plain-qro/
              cp -r ${conformance-transport-plain-plain}/. $out/plain-plain/
              cp -r ${conformance-transport-released-plain}/. $out/released-plain/
              cp -r ${conformance-transport-plain-released}/. $out/plain-released/
              cp -r ${conformance-transport-released-daemon}/. $out/released-daemon/
              cp -r ${conformance-transport-ext-plain}/. $out/ext-plain/
              cp -r ${conformance-transport-inproc}/. $out/inproc-plain/
              cp -r ${conformance-transport-peered}/. $out/peered/
              echo ${released-modules-unchanged} > $out/released-modules-unchanged
            '';

          # ── the merged report ───────────────────────────────────────────
          # ONE page, BOTH contracts, and nothing computed across them. A
          # view-layer merge: it re-runs no measurement, it reads what the two
          # runs above already wrote (each page carries its payload in
          # `<script id="report-data">`) and lays them out as two labelled
          # contracts on one page.
          #
          # A third DERIVATION rather than a third run, and the two gates stay
          # separate on purpose: they measure different contracts against
          # different providers, and one going red must not suppress the
          # other's report. The consequence is that this derivation cannot
          # build while either gate is red — which is right for a `check`, and
          # exactly why CI does NOT get its landing page from here: CI merges
          # the uploaded ARTIFACTS with the same script (see
          # .github/workflows/conformance.yml), so the merged page still exists
          # on the run where it is most worth reading.
          #
          # $out is laid out as the published site, so `nix build` produces
          # something serveable as-is: index.html is the merged page, and
          # full/ + ext/ are the standalone reports it links to.
          conformance-matrix-merged =
            pkgs.runCommand "logoscore-py-conformance-matrix-merged" {
              nativeBuildInputs = [ python ];
            } ''
              mkdir -p $out/full $out/ext
              cp ${conformance-matrix}/matrix.html     $out/full/index.html
              cp ${conformance-matrix}/matrix.jsonl    $out/full/matrix.jsonl
              cp ${conformance-matrix}/matrix.txt      $out/full/matrix.txt
              cp ${conformance-matrix}/known-broken.md $out/full/known-broken.md
              cp ${conformance-matrix-ext}/matrix-ext.html  $out/ext/index.html
              cp ${conformance-matrix-ext}/matrix-ext.jsonl $out/ext/matrix.jsonl
              cp ${conformance-matrix-ext}/matrix-ext.txt   $out/ext/matrix.txt
              chmod -R u+w $out
              ${python}/bin/python ${./conformance/matrix_report.py} \
                --merge $out/full/index.html $out/ext/index.html \
                --href  ./full/ ./ext/ \
                -o $out/index.html
            '';

          # The client reaches its daemons over the local socket; a daemon
          # elsewhere is Remote Runtime Control (test_runtime_control.py).
          integration-local          = mkIntegration;
          integration-logosctl-local = mkIntegrationLogosctl;

          # Back-compat alias — equivalent to `integration-local`. Kept
          # so anyone with `nix build .#checks.<system>.integration` in
          # muscle memory still gets a green path.
          integration = mkIntegration;
        }
      );
    };
}
