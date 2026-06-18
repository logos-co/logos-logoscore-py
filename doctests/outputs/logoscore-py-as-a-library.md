# Driving logoscore from Python with logos-logoscore-py

[`logos-logoscore-py`](https://github.com/logos-co/logos-logoscore-py) is the **Python
wrapper** for the [`logoscore`](https://github.com/logos-co/logos-logoscore-cli) CLI.
It lets you launch a daemon, load modules, call methods, and subscribe to events from
Python — without shelling out and parsing output by hand. Every operation spawns a
`logoscore <subcommand> --json` subprocess and parses its output; there are no C++
bindings.

This doc-test exercises **this** wrapper commit end-to-end:

1. Build the package from its flake — the build runs `pythonImportsCheck`, so a
   successful build already proves `import logoscore` works for this commit.
2. Enter the repo's own `nix develop` shell, which puts the `logoscore` CLI on `PATH`
   and exports `LOGOSCORE_BIN` + `LOGOSCORE_TEST_MODULES_DIR` (a prebuilt
   `test_basic_module` install tree).
3. Run a real Python program that uses the documented API: spawn a headless daemon with
   `LogoscoreDaemon`, connect a client, load `test_basic_module`, call several of its
   `Q_INVOKABLE` methods, and round-trip an event through `on_event`.

Because the wrapper, the CLI, and the test module all resolve through this commit's
flake, a green run is direct evidence that this `logos-logoscore-py` commit drives a
real daemon from Python.

**What you'll build:** This commit of `logos-logoscore-py`, used from a Python script to spawn a headless `logoscore` daemon and drive `test_basic_module` — method calls and an event round-trip.

**What you'll learn:**

- How `logos-logoscore-py` ships as a Python package whose `nix build` proves `import logoscore`
- How the repo's dev shell wires `logoscore` + a test-modules dir onto the environment
- How to spawn a headless daemon with `LogoscoreDaemon` and connect a client
- How to load a module, call its methods, and subscribe to an event from Python

## Prerequisites

- **Nix** with flakes enabled. Install from [nixos.org](https://nixos.org/download.html), then enable flakes:

```bash
mkdir -p ~/.config/nix
echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf
```

Verify: `nix flake --help >/dev/null 2>&1 && echo "Flakes enabled"`

- **A Linux or macOS machine.** The daemon loads Qt module plugins headlessly via `QT_QPA_PLATFORM=offscreen` (the script sets it), so no display is required.

---

## Step 1: Build the package

Build this commit of `logos-logoscore-py` from its flake. The package is a Python
wheel whose build runs `pythonImportsCheck = ["logoscore"]`, so a successful build
already proves the module imports.

> The `` in the URL pins the build to the commit under test: the doc-test
> runner expands it to a concrete ref (locally this checkout's `HEAD` — see
> `run.sh`; in CI the commit being tested). With no pin it falls back to the latest
> `master`. Developing against a local checkout? Replace the GitHub reference with
> `.`, e.g. `nix build '.' -o result`.

### 1.1 Build the wheel (runs the import check)

```bash
nix build 'github:logos-co/logos-logoscore-py' -o result
```

The build's `pythonImportsCheck` imports `logoscore`, so reaching this point means
the package built and imports cleanly.

---

## Step 2: Confirm the dev shell wiring

The repo's `nix develop` shell puts `logoscore` on `PATH` and exports the two
environment variables the Python program below relies on: `LOGOSCORE_BIN` (the CLI
binary) and `LOGOSCORE_TEST_MODULES_DIR` (a prebuilt `test_basic_module` install
tree). Confirm all three are present.

### 2.1 Check the CLI and env vars

```bash
nix develop 'github:logos-co/logos-logoscore-py' --command bash -c '
  logoscore --version && echo CLI_OK
  echo "BIN=$LOGOSCORE_BIN"
  test -d "$LOGOSCORE_TEST_MODULES_DIR" && echo MODULES_OK
'
```

---

## Step 3: Write the driver program

This program uses only the documented API. It sets `QT_QPA_PLATFORM=offscreen` (the
daemon loads Qt plugins, and the dev shell doesn't set this for you), spawns a
headless daemon over `LOGOSCORE_TEST_MODULES_DIR`, connects a client, loads
`test_basic_module`, calls several of its methods, and round-trips an event. The
expected return values are the same ones the repo's integration suite pins.

### 3.1 Write drive_logoscore.py

```python
import os, threading, time

# The daemon loads Qt module plugins; force the headless platform before
# anything spawns it. The dev shell puts logoscore on PATH but does not set
# this, so we set it here (mirroring the repo's integration check).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from logoscore import LogoscoreDaemon

BIN = os.environ["LOGOSCORE_BIN"]
MODULES = os.environ["LOGOSCORE_TEST_MODULES_DIR"]
MOD = "test_basic_module"

with LogoscoreDaemon(modules_dir=MODULES, binary=BIN) as daemon:
    client = daemon.client()

    client.load_module(MOD)

    info = client.module_info(MOD)
    method_names = {m["name"] for m in info["methods"]}
    assert "addInts" in method_names, method_names
    print("module-info methods include addInts:", "addInts" in method_names)

    # Each value below is pinned by the repo's integration suite
    # (tests/_basic_module_cases.py / tests/integration/).
    assert client.call(MOD, "addInts", 2, 3) == 5
    print("addInts(2,3) =", client.call(MOD, "addInts", 2, 3))

    assert client.call(MOD, "returnString") == "test_basic_module"
    print("returnString =", client.call(MOD, "returnString"))

    assert client.call(MOD, "concat", "foo", "bar") == "foobar"
    print("concat =", client.call(MOD, "concat", "foo", "bar"))

    assert client.call(MOD, "splitString", "a,b,c") == ["a", "b", "c"]
    print("splitString =", client.call(MOD, "splitString", "a,b,c"))

    # Event round-trip: subscribe, fire emitTestEvent, wait for the callback.
    received: list[dict] = []
    got = threading.Event()

    def on_event(evt: dict) -> None:
        received.append(evt)
        got.set()

    with client.on_event(MOD, "testEvent", on_event):
        time.sleep(0.5)  # let the watcher subscribe before firing
        client.call(MOD, "emitTestEvent", "hello from python")
        assert got.wait(timeout=10.0), "event not received within 10s"

    payload = received[0]
    assert any("hello from python" in str(v) for v in payload.values())
    print("event payload contained: hello from python")

print("ALL CHECKS PASSED")
```

The `with LogoscoreDaemon(...)` block spawns `logoscore -D` with an isolated
config dir and tears it down on exit. `daemon.client()` reads the per-module
endpoints the daemon wrote at startup.

---

## Step 4: Run the driver

Run the program inside the dev shell so `logoscore` and the test-modules dir are on
the environment. We point `PYTHONPATH` at the `logoscore` package we built in step 1
(its site-packages under `./result`) so `import logoscore` resolves — `nix develop
--command` runs non-interactively and doesn't apply the dev shell's
`PYTHONPATH=$PWD/src` hook. A clean exit with `ALL CHECKS PASSED` is the end-to-end
proof.

### 4.1 Run drive_logoscore.py

```bash
# ./result is the package built in step 1; add its site-packages to PYTHONPATH
SITE_PACKAGES="$(dirname "$(dirname "$(find -L result -name '__init__.py' -path '*logoscore*' | head -n1)")")"
nix develop 'github:logos-co/logos-logoscore-py' --command \
  env PYTHONPATH="$SITE_PACKAGES" QT_QPA_PLATFORM=offscreen python drive_logoscore.py
```

---

## Recap

You drove a real `logoscore` daemon entirely from Python with this wrapper commit:

| Step | API used | Proves |
|---|---|---|
| build | `nix build` + `pythonImportsCheck` | the package imports |
| spawn | `LogoscoreDaemon(...)` | a headless daemon starts with an isolated config |
| call | `client.call(mod, method, *args)` | method calls round-trip real values |
| events | `client.on_event(...)` | event subscription + delivery works |

Because the wrapper, the CLI, and `test_basic_module` all resolve through the commit
under test, a green run proves this `logos-logoscore-py` commit drives a daemon from
Python end-to-end.
