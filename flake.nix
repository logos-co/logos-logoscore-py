{
  description = "Python wrapper for the logoscore CLI — launch daemons, load modules, call methods, subscribe to events";

  inputs = {
    logos-nix.url = "github:logos-co/logos-nix";
    nixpkgs.follows = "logos-nix/nixpkgs";

    logos-logoscore-cli.url = "github:logos-co/logos-logoscore-cli";
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
        in {
          default = pythonPkg;
          logoscore-py = pythonPkg;
        }
      );

      # ── Dev shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into a shell with python + pytest + logoscore.
      devShells = forAllSystems ({ pkgs, system }: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pytest ]))
            logos-logoscore-cli.packages.${system}.default
          ];

          shellHook = ''
            echo "logos-logoscore-py dev shell"
            echo "  python:    $(python --version)"
            echo "  logoscore: $(logoscore --version 2>/dev/null || echo 'not on PATH')"
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
          testModules = logos-test-modules.packages.${system}.default;
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
            export LOGOSCORE_TEST_MODULES_DIR=${testModules}/lib
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
