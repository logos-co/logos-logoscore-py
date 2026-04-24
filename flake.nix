{
  description = "Python wrapper for the logoscore CLI — launch daemons, load modules, call methods, subscribe to events";

  inputs = {
    logos-nix.url = "github:logos-co/logos-nix";
    nixpkgs.follows = "logos-nix/nixpkgs";

    logos-logoscore-cli.url = "github:logos-co/logos-logoscore-cli/support-non-local-remote-transports";
    logos-logoscore-cli.inputs.logos-nix.follows = "logos-nix";
    logos-logoscore-cli.inputs.nixpkgs.follows = "nixpkgs";

    logos-test-modules.url = "github:logos-co/logos-test-modules";
    logos-test-modules.inputs.logos-nix.follows = "logos-nix";
    logos-test-modules.inputs.logos-logoscore-cli.follows = "logos-logoscore-cli";
    logos-test-modules.inputs.nixpkgs.follows = "nixpkgs";
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
      # `dockerImage` (Linux only, cross-build from macOS) bundles the CLI +
      # test_basic_module into an image consumed by the docker smoke tests.
      # Load with: `docker load < $(nix build .#dockerImage --no-link --print-out-paths)`.
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
          # Two flavors for the docker smoke image:
          #
          #   * `dockerBundle` (dev) — uses `.#cli` (the default logoscore
          #     package, which links against Qt/Boost/OpenSSL from the nix
          #     store via rpath) + `.install` test modules (same). Smaller
          #     bundle (~60 MB), but the runtime image MUST ship the nix
          #     store so those rpaths resolve. This is what the
          #     `logoscore-py` dev shell uses.
          #
          #   * `dockerBundlePortable` — uses `.#cli-bundle-dir` (a
          #     self-contained bin/ + lib/ + modules/ tree with every Qt
          #     dep copied in) + `.install-portable` test modules. Larger
          #     (~400 MB), but runs standalone — the same shape a released
          #     portable binary would have.
          #
          # The Dockerfile picks one via `--build-arg FLAVOR=dev|portable`
          # and the pytest suite parametrises over both so regressions in
          # either flavor surface in the smoke test matrix.

          testBasicInstall         = logos-test-modules.modules.${system}.test_basic_module.install;
          testBasicInstallPortable = logos-test-modules.modules.${system}.test_basic_module.install-portable;
          logoscorePortable        = logos-logoscore-cli.packages.${system}.cli-bundle-dir;

          dockerBundle = pkgs.runCommand "logoscore-bundle-dev" { } ''
            mkdir -p $out/bin $out/modules
            cp ${logoscoreBin}/bin/logoscore $out/bin/
            cp -r ${testBasicInstall}/modules/* $out/modules/
          '';

          dockerBundlePortable = pkgs.runCommand "logoscore-bundle-portable" { } ''
            mkdir -p $out/modules
            # cli-bundle-dir already is a self-contained bin/ + lib/ +
            # modules/ tree; copy the whole thing as the image root,
            # then overlay the test modules on top of it.
            cp -r ${logoscorePortable}/* $out/
            chmod -R u+w $out
            cp -r ${testBasicInstallPortable}/modules/* $out/modules/
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
          logoscoreBin = logos-logoscore-cli.packages.${system}.default;
          testBasicInstall = logos-test-modules.modules.${system}.test_basic_module.install;
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
          LOGOSCORE_BIN              = "${logoscoreBin}/bin/logoscore";
          LOGOSCORE_TEST_MODULES_DIR = "${testBasicInstall}/modules";

          shellHook = ''
            echo "logos-logoscore-py dev shell"
            echo "  python:              $(python --version)"
            echo "  logoscore:           $(logoscore --version 2>/dev/null || echo 'not on PATH')"
            echo "  LOGOSCORE_BIN:       $LOGOSCORE_BIN"
            echo "  test_modules dir:    $LOGOSCORE_TEST_MODULES_DIR"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
          '';
        };
      });

      # ── Checks ────────────────────────────────────────────────────────────
      # `nix flake check` runs the unit tests (no daemon required) and the
      # integration test suite against a real logoscore + test modules.
      checks = forAllSystems ({ pkgs, system }:
        let
          python = pkgs.python3.withPackages (ps: [ ps.pytest ]);
          logoscoreBin = logos-logoscore-cli.packages.${system}.default;
          # `.install` lays out modules/<name>/<name>_plugin.{so,dylib} +
          # manifest.json — the layout logoscore's `-m` flag expects.
          testBasicInstall = logos-test-modules.modules.${system}.test_basic_module.install;
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

          integration = pkgs.runCommand "logoscore-py-integration-tests" {
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
            export LOGOSCORE_TEST_MODULES_DIR=${testBasicInstall}/modules
            # Run from a writable HOME so any stray ~/.logoscore writes are isolated.
            export HOME=$PWD/home
            mkdir -p $HOME
            ${python}/bin/pytest tests/integration -v
            touch $out
          '';
        }
      );
    };
}
