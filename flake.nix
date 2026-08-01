{
  description = "Python wrappers for the logoscore and logosctl CLIs — launch daemons, load modules, call methods, subscribe to events";

  inputs = {
    logos-nix.url = "github:logos-co/logos-nix";
    nixpkgs.follows = "logos-nix/nixpkgs";
    logos-logoscore-cli.url = "github:logos-co/logos-logoscore-cli";
    # ── TEMPORARY: the logosctl CLI, pre-merge ─────────────────────────────
    # A SECOND pin of the same repo, at the unmerged branch that introduces
    # the `ctl` output. It exists so adding the logosctl client here stays
    # genuinely additive: the input above keeps pointing at master, so every
    # pre-existing `logoscore` output — the wheel, the docker bundles, the
    # `unit` / `integration-*` / `conformance-*` checks — builds against
    # exactly the CLI commit it built against before, and the new CLI's
    # blast radius is confined to the `*-logosctl` checks and the dev shell.
    #
    # REMOVE WHEN: logos-logoscore-cli#feat/logosctl-cli-migration merges to
    # master. Then `ctl` exists on the input above, and the cleanup is
    # deleting this input, its `outputs` argument, and repointing the two
    # `logosctlBin` bindings at `logos-logoscore-cli`.
    logos-logoscore-cli-ctl.url = "github:logos-co/logos-logoscore-cli/feat/logosctl-cli-migration";
    logos-test-modules.url = "github:logos-co/logos-test-modules";
  };

  outputs = { self, nixpkgs, logos-logoscore-cli, logos-logoscore-cli-ctl, logos-test-modules, ... }:
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
      # `dockerBundle` / `dockerBundlePortable` (Linux only) prepare an
      # `out/bundle` directory consumed by `tests/docker_smoke/Dockerfile`
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
            # ships the `logosctl` client. Propagating `ctl` would put the
            # under-validation binary on the critical path of `nix build` —
            # the one output every consumer of this flake pulls — so a hiccup
            # in the new CLI could redden the old client's build. Anyone who
            # wants the binary takes it from the `logos-logoscore-cli-ctl`
            # input explicitly (the dev shell and the logosctl checks below do
            # exactly that) — which is also why this package, and every other
            # pre-existing output, still resolves through the master-pinned
            # `logos-logoscore-cli` input alone.
            propagatedBuildInputs = [ logoscoreBin ];
            doCheck = false;
            pythonImportsCheck = [ "logoscore" "logosctl" ];
          };

          # ── Docker bundles ─────────────────────────────────────────────
          # The bundle is **just the logoscore CLI** plus whatever modules
          # it ships with (currently capability_module, package_manager_module).
          # No test modules — those get bind-mounted at runtime via
          # `-v $modules_dir:/user-modules` and `-m /user-modules`. That
          # makes the image reusable for anyone who wants to test their
          # own module: pull the image, `docker run -v ./my-modules:/user-modules
          # logoscore:smoke-dev daemon -m /user-modules …`.
          #
          # Two flavors:
          #
          #   * `dockerBundle` (dev) — uses `.#cli` (the default logoscore
          #     package, which links against Qt/Boost/OpenSSL from the nix
          #     store via rpath). Smaller bundle (~60 MB payload) but the
          #     runtime image MUST ship the nix store so those rpaths
          #     resolve, and the CLI's built-in modules are found via
          #     LOGOS_BUNDLED_MODULES_DIR (set by `wrapQtAppsNoGuiHook`
          #     when the CLI was built). This is the flavor the
          #     `logoscore-py` dev shell matches.
          #
          #   * `dockerBundlePortable` — uses `.#cli-bundle-dir` (a
          #     self-contained `bin/ + lib/ + modules/` tree with every
          #     Qt dep + the CLI's built-in modules copied in). Larger
          #     (~400 MB) but runs standalone — no nix store needed. The
          #     CLI's built-in modules live at `/opt/logoscore/modules`
          #     and are discovered by explicitly passing `-m
          #     /opt/logoscore/modules` (no wrapper env var here).
          #
          # The Dockerfile picks one via `--build-arg FLAVOR=dev|portable`.
          # The pytest suite parametrises over both flavors so regressions
          # in either path surface in the smoke matrix.

          logoscorePortable = logos-logoscore-cli.packages.${system}.cli-bundle-dir;

          dockerBundle = pkgs.runCommand "logoscore-bundle-dev" { } ''
            # Dev flavor: just the binary. rpath points into /nix/store
            # (copied wholesale in Dockerfile stage 2), and the
            # wrapped binary carries `LOGOS_BUNDLED_MODULES_DIR` baked
            # in — pointing at the CLI's own modules dir in the store —
            # so capability_module etc. resolve without extra `-m` flags.
            mkdir -p $out/bin
            cp ${logoscoreBin}/bin/logoscore $out/bin/
          '';

          dockerBundlePortable = pkgs.runCommand "logoscore-bundle-portable" { } ''
            # Portable flavor: cli-bundle-dir is already a self-contained
            # bin/ + lib/ + modules/ tree — the CLI's own built-in
            # modules live under its modules/ subdir. Copy it as-is.
            mkdir -p $out
            cp -r ${logoscorePortable}/* $out/
            chmod -R u+w $out
          '';
        in {
          default              = pythonPkg;
          logoscore-py         = pythonPkg;
          dockerBundle         = dockerBundle;
          dockerBundlePortable = dockerBundlePortable;
        }
      );

      # ── Dev shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into a shell with python + pytest + logoscore
      # + a pre-built test_fullapi_cpp install tree, so `pytest` just works
      # without any extra environment setup. The nix `integration` check
      # sets the same two env vars, so the dev shell matches CI behaviour.
      devShells = forAllSystems ({ pkgs, system }:
        let
          logoscoreBin             = logos-logoscore-cli.packages.${system}.default;
          # Same repo, a SECOND input: `ctl` only exists on the unmerged CLI
          # branch, so it is taken from `logos-logoscore-cli-ctl` and never
          # from the master pin above — see the input's comment. Both binaries
          # are on PATH here so a plain `pytest` covers both suites —
          # tests/logosctl skips silently when LOGOSCTL_BIN is unset, which
          # would make the new suite look green while never running.
          logosctlBin              = logos-logoscore-cli-ctl.packages.${system}.ctl;
          # `test_fullapi_cpp` (universal C++) is the single test module the
          # suite loads — its methods + typed events span the whole
          # parameter/return/event surface. `.install` lays out
          # modules/<name>/… ready for the daemon's `-m` flag.
          testModulesInstall         = logos-test-modules.modules.${system}.test_fullapi_cpp.install;
          testModulesInstallPortable = logos-test-modules.modules.${system}.test_fullapi_cpp.install-portable;
        in {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ]))
            logoscoreBin
            logosctlBin
          ];

          # Integration tests skip when these are unset (by design, so
          # `pytest` on a plain Python env doesn't try to spawn daemons).
          # Exporting them here means the dev shell exercises the full
          # suite out of the box.
          #
          # Two module-dir vars because the docker smoke flavors differ:
          # the `dev` image has /nix/store so `.install` modules (which
          # rpath into the store) work; the `portable` image is standalone
          # so we need `.install-portable` (self-contained). The docker
          # smoke fixture picks the right one per flavor.
          LOGOSCORE_BIN                       = "${logoscoreBin}/bin/logoscore";
          LOGOSCORE_TEST_MODULES_DIR          = "${testModulesInstall}/modules";
          LOGOSCORE_TEST_MODULES_DIR_PORTABLE = "${testModulesInstallPortable}/modules";

          # The logosctl suite reads its own pair of variables (its conftest
          # rebinds `test_modules_dir` to LOGOSCTL_TEST_MODULES_DIR) so a
          # machine can point the two suites at different builds. Here they
          # are the same modules — the module ABI is shared, only the CLI differs.
          LOGOSCTL_BIN                        = "${logosctlBin}/bin/logosctl";
          LOGOSCTL_TEST_MODULES_DIR           = "${testModulesInstall}/modules";

          shellHook = ''
            echo "logos-logoscore-py dev shell"
            echo "  python:                                  $(python --version)"
            echo "  logoscore:                               $(logoscore --version 2>/dev/null || echo 'not on PATH')"
            echo "  logosctl:                                $(logosctl --version 2>/dev/null || echo 'not on PATH')"
            echo "  LOGOSCORE_BIN:                           $LOGOSCORE_BIN"
            echo "  LOGOSCORE_TEST_MODULES_DIR (dev):        $LOGOSCORE_TEST_MODULES_DIR"
            echo "  LOGOSCORE_TEST_MODULES_DIR_PORTABLE:     $LOGOSCORE_TEST_MODULES_DIR_PORTABLE"
            echo "  LOGOSCTL_BIN:                            $LOGOSCTL_BIN"
            echo "  LOGOSCTL_TEST_MODULES_DIR:               $LOGOSCTL_TEST_MODULES_DIR"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        };
      });

      # ── Checks ────────────────────────────────────────────────────────────
      # `nix flake check` runs the unit tests (no daemon required) and the
      # integration test suite against a real CLI + test modules.
      #
      # Both suites exist twice, once per client: `unit` / `integration-*`
      # drive `logoscore`, `unit-logosctl` / `integration-logosctl-*` drive
      # `logosctl`. Separate derivations throughout, never one derivation
      # looping over both — see the `unit-logosctl` comment.
      #
      # The integration suite is replicated across three transports so a
      # regression in tcp framing or tcp_ssl handshaking surfaces at the
      # same layer the test names already cover. Three separate flake
      # outputs (rather than one derivation that loops) so:
      #   - CI can fan them out across runners in parallel,
      #   - a tcp_ssl failure doesn't block the local/tcp signal,
      #   - the build log of any single transport stays focused.
      checks = forAllSystems ({ pkgs, system }:
        let
          python = pkgs.python3.withPackages (ps: [ ps.pytest ]);
          logoscoreBin = logos-logoscore-cli.packages.${system}.default;
          # The `logosctl` binary, from the SECOND CLI input — the unmerged
          # branch is the only place a `ctl` output exists. Keeping it on its
          # own input is what makes this change additive: `logoscoreBin` above
          # still resolves through the master-pinned input, so the checks that
          # existed before this branch build against precisely the CLI commit
          # they built against before. Referenced only from the `*-logosctl`
          # checks below, so the new CLI is never on the evaluation path of a
          # logoscore check either.
          logosctlBin = logos-logoscore-cli-ctl.packages.${system}.ctl;
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
          # composites) has ONE provider today — the C++ cdylib backend cannot
          # express these types yet — so its table runs without a differential.
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

          # Helper: run the integration suite once with the given
          # `--transport` value. Same env wiring as the unit check
          # plus openssl (needed by the `self_signed_cert` fixture
          # for `tcp_ssl`; harmless for `local` / `tcp`).
          mkIntegration = transport: pkgs.runCommand
            "logoscore-py-integration-tests-${transport}" {
              nativeBuildInputs = [ python logoscoreBin pkgs.openssl ]
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
              ${python}/bin/pytest tests/integration -v --transport=${transport}
              touch $out
            '';

          # Helper: the same thing for the logosctl suite. A sibling rather
          # than a `binary:`/`suite:` parameter on `mkIntegration`, for the
          # reason the two test trees are duplicated in the first place — the
          # two CLIs configure a daemon through different mechanisms, and
          # retiring logoscore should be a delete, not an untangle. The two
          # helpers drifting apart is expected, not a smell.
          mkIntegrationLogosctl = transport: pkgs.runCommand
            "logosctl-py-integration-tests-${transport}" {
              nativeBuildInputs = [ python logosctlBin pkgs.openssl ]
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
              # tests/logosctl/conftest.py SKIPS when either of those is unset,
              # so a rename here turns the whole suite green-by-omission.
              #
              # Writable HOME: logosctl defaults to ~/.logosctl and refuses to
              # start a second daemon in a config dir that already holds a live
              # one. Every test drives an isolated --config-dir, but a stray
              # default-session write must still land somewhere sandbox-local.
              export HOME=$PWD/home
              mkdir -p $HOME
              ${python}/bin/pytest tests/logosctl/integration -v --transport=${transport}
              touch $out
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
          # logosctl (the CLI still under validation) cannot mask a logoscore
          # regression. Deleting logosctl later is deleting these four
          # attributes, `mkIntegrationLogosctl`, `logosctlBin`, and the
          # `logos-logoscore-cli-ctl` input.
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
            nativeBuildInputs = [ python logoscoreBin pkgs.openssl ]
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

          conformance-matrix-ext = pkgs.runCommand "logoscore-py-conformance-matrix-ext" {
            nativeBuildInputs = [ python logoscoreBin pkgs.openssl ]
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
              --modules test_fullapi_ext_rust=${testModulesExtInstall}/modules \
              --modules test_fullapi_ext_cpp=${testModulesExtCppInstall}/modules \
              --jsonl $out/matrix-ext.jsonl \
              --report $out/matrix-ext.html \
              --no-color \
              2>&1 | tee $out/matrix-ext.txt
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

          # One check per transport. CI's matrix fans them out; a local
          # `nix flake check` runs all three sequentially.
          integration-local   = mkIntegration "local";
          integration-tcp     = mkIntegration "tcp";
          integration-tcp_ssl = mkIntegration "tcp_ssl";

          # Same three transports, driven through logosctl. Split the same way
          # and for the same reasons.
          integration-logosctl-local   = mkIntegrationLogosctl "local";
          integration-logosctl-tcp     = mkIntegrationLogosctl "tcp";
          integration-logosctl-tcp_ssl = mkIntegrationLogosctl "tcp_ssl";

          # Back-compat alias — equivalent to `integration-local`. Kept
          # so anyone with `nix build .#checks.<system>.integration` in
          # muscle memory still gets a green path.
          integration = mkIntegration "local";
        }
      );
    };
}
