{
  description = "Python wrapper for the logoscore CLI — launch daemons, load modules, call methods, subscribe to events";

  inputs = {
    logos-nix.url = "github:logos-co/logos-nix";
    nixpkgs.follows = "logos-nix/nixpkgs";
    logos-logoscore-cli.url = "github:logos-co/logos-logoscore-cli";
    logos-test-modules.url = "github:logos-co/logos-test-modules";
  };

  outputs = { self, nixpkgs, logos-logoscore-cli, logos-test-modules, ... }:
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
            propagatedBuildInputs = [ logoscoreBin ];
            doCheck = false;
            pythonImportsCheck = [ "logoscore" ];
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
      # + a pre-built test_basic_module install tree, so `pytest` just works
      # without any extra environment setup. The nix `integration` check
      # sets the same two env vars, so the dev shell matches CI behaviour.
      devShells = forAllSystems ({ pkgs, system }:
        let
          logoscoreBin             = logos-logoscore-cli.packages.${system}.default;
          testBasicInstall         = logos-test-modules.modules.${system}.test_basic_module.install;
          testBasicCppInstall      = logos-test-modules.modules.${system}.test_basic_module_cpp.install;
          testBasicInstallPortable = logos-test-modules.modules.${system}.test_basic_module.install-portable;
          testBasicCppInstallPortable = logos-test-modules.modules.${system}.test_basic_module_cpp.install-portable;

          # Merge every test module's `.install` output into one dir so a
          # single `-m` flag on the daemon picks them all up. Each module's
          # install output already has the shape `modules/<name>/…`, so
          # symlinkJoin stacks them without collision.
          testModulesInstall = pkgs.symlinkJoin {
            name = "logoscore-py-test-modules";
            paths = [ testBasicInstall testBasicCppInstall ];
          };
          testModulesInstallPortable = pkgs.symlinkJoin {
            name = "logoscore-py-test-modules-portable";
            paths = [ testBasicInstallPortable testBasicCppInstallPortable ];
          };
        in {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ]))
            logoscoreBin
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

          shellHook = ''
            echo "logos-logoscore-py dev shell"
            echo "  python:                                  $(python --version)"
            echo "  logoscore:                               $(logoscore --version 2>/dev/null || echo 'not on PATH')"
            echo "  LOGOSCORE_BIN:                           $LOGOSCORE_BIN"
            echo "  LOGOSCORE_TEST_MODULES_DIR (dev):        $LOGOSCORE_TEST_MODULES_DIR"
            echo "  LOGOSCORE_TEST_MODULES_DIR_PORTABLE:     $LOGOSCORE_TEST_MODULES_DIR_PORTABLE"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        };
      });

      # ── Checks ────────────────────────────────────────────────────────────
      # `nix flake check` runs the unit tests (no daemon required) and the
      # integration test suite against a real logoscore + test modules.
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
          # `.install` lays out modules/<name>/<name>_plugin.{so,dylib} +
          # manifest.json — the layout logoscore's `-m` flag expects.
          # Merge `test_basic_module` (Qt) + `test_basic_module_cpp` (pure-C++)
          # into one dir so a single `-m` flag loads both — the integration
          # suite has separate test files per module.
          testBasicInstall    = logos-test-modules.modules.${system}.test_basic_module.install;
          testBasicCppInstall = logos-test-modules.modules.${system}.test_basic_module_cpp.install;
          testModulesInstall  = pkgs.symlinkJoin {
            name = "logoscore-py-test-modules";
            paths = [ testBasicInstall testBasicCppInstall ];
          };

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
        in
        {
          unit = pkgs.runCommand "logoscore-py-unit-tests" {
            nativeBuildInputs = [ python ];
          } ''
            cp -r ${./.}/. .
            chmod -R +w .
            export PYTHONPATH=$PWD/src
            ${python}/bin/pytest tests/unit -v
            touch $out
          '';

          # One check per transport. CI's matrix fans them out; a local
          # `nix flake check` runs all three sequentially.
          integration-local   = mkIntegration "local";
          integration-tcp     = mkIntegration "tcp";
          integration-tcp_ssl = mkIntegration "tcp_ssl";

          # Back-compat alias — equivalent to `integration-local`. Kept
          # so anyone with `nix build .#checks.<system>.integration` in
          # muscle memory still gets a green path.
          integration = mkIntegration "local";
        }
      );
    };
}
