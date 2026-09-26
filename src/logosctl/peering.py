"""Two logosctl daemons on one machine, one calling the other's modules.

`PeeredDaemons` starts an exporting daemon and an importing one, pairs them
through the exporter's local invite, and imports the exported modules on the
importer. There each import loads as a facade that forwards every call, and
every event, to the exporter's copy of the module.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .client import LogosctlClient
from .daemon import LogosctlDaemon
from .errors import LogosctlError


class PeeredDaemons:
    """Context manager: `exporter` shares `exports` with `importer`.

    The exporter's local invite grants the importer every export, and each
    import admits `allowed_callers` on the importer ("*": any caller, the
    `logosctl call` operator included). Both daemons keep their config dirs
    under one temp dir, so `restart_exporter()` brings back the same runtime.
    """

    def __init__(
        self,
        modules_dir: str | Path | list[str | Path],
        exports: Iterable[str],
        *,
        binary: str = "logosctl",
        importer_modules_dir: str | Path | list[str | Path] | None = None,
        events: bool = True,
        allowed_callers: Iterable[str] = ("*",),
        startup_timeout: float = 60.0,
        timeout: float = 60.0,
    ) -> None:
        self.modules_dir = modules_dir
        self.exports = list(exports)
        if not self.exports:
            raise ValueError("at least one export is required")
        self.binary = binary
        self.importer_modules_dir = importer_modules_dir
        self.events = events
        self.allowed_callers = list(allowed_callers)
        self.startup_timeout = startup_timeout
        self.timeout = timeout

        self._root: Path | None = None
        self._exporter: LogosctlDaemon | None = None
        self._importer: LogosctlDaemon | None = None
        self.exporter_id = ""
        self.importer_id = ""

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._root is not None:
            raise LogosctlError("peered daemons already started")
        self._root = Path(tempfile.mkdtemp(prefix="peered-"))
        try:
            self._start_exporter()
            self.exporter_id = self.exporter_client().peer("status")["runtime_id"]

            empty = self._root / "importer-modules"
            empty.mkdir()
            self._importer = LogosctlDaemon(
                self.importer_modules_dir or empty,
                binary=self.binary,
                config_dir=self._root / "importer",
                extra_config={"peering": self.importer_config()},
                startup_timeout=self.startup_timeout,
            )
            self._importer.start()
            self.importer_id = self.importer_client().peer("status")["runtime_id"]

            self._link()
            for name in self.exports:
                self.import_module(name)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Stop the importer first, so its facades go before what they call."""
        for daemon in (self._importer, self._exporter):
            if daemon is not None:
                daemon.stop()
        self._importer = self._exporter = None
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def stop_exporter(self) -> None:
        """Stop the exporter alone; its state stays for `start_exporter()`."""
        if self._exporter is not None:
            self._exporter.stop()
            self._exporter = None

    def start_exporter(self) -> None:
        """Start the exporter on its kept state: the same runtime ID, pairing
        and control port. Its exports load again."""
        if self._root is None:
            raise LogosctlError("peered daemons are not running")
        if self._exporter is not None:
            raise LogosctlError("the exporter is already running")
        self._start_exporter()

    def restart_exporter(self) -> None:
        self.stop_exporter()
        self.start_exporter()

    def __enter__(self) -> "PeeredDaemons":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── Accessors ───────────────────────────────────────────────────────────

    @property
    def exporter(self) -> LogosctlDaemon:
        if self._exporter is None:
            raise LogosctlError("the exporter is not running")
        return self._exporter

    @property
    def importer(self) -> LogosctlDaemon:
        if self._importer is None:
            raise LogosctlError("the importer is not running")
        return self._importer

    def exporter_client(self) -> LogosctlClient:
        return self.exporter.client()

    def importer_client(self) -> LogosctlClient:
        return self.importer.client()

    # ── Configuration ───────────────────────────────────────────────────────

    def exporter_config(self) -> dict[str, Any]:
        """The exporter's `peering:` section: a loopback control endpoint on a
        port it picks and keeps, a local invite that grants every export, and
        the exports themselves."""
        return {
            "name": "exporter",
            "control": {"enabled": True, "host": "127.0.0.1", "port": 0,
                        "local_invite": {"allow": ["*"]}},
            "exports": {"enabled": True,
                        "modules": {name: {"events": self.events} for name in self.exports}},
        }

    def importer_config(self) -> dict[str, Any]:
        """The importer only dials out, so it needs no control endpoint."""
        return {"name": "importer"}

    # ── Imports, policy ─────────────────────────────────────────────────────

    def import_module(
        self,
        name: str,
        *,
        module: str | None = None,
        allowed_callers: Iterable[str] | None = None,
        events: bool | None = None,
        wait: bool = True,
    ) -> None:
        """Import the exporter's `module` (default `name`) as `name` on the
        importer; with `wait`, until the import is ready."""
        args = [name, "--from", self.exporter_id,
                "--allow", ",".join(allowed_callers or self.allowed_callers)]
        if module is not None:
            args += ["--module", module]
        if self.events if events is None else events:
            args.append("--events")
        self.importer_client().peer("import", *args)
        if wait:
            self.wait_for_import(name)

    def import_state(self, name: str) -> dict[str, Any]:
        """`{loaded, state, reason}` of an import on the importer."""
        imports = self.importer_client().peer("routes").get("imports", {})
        return imports.get(name, {})

    def wait_for_import(self, name: str, *states: str,
                        timeout: float | None = None) -> dict[str, Any]:
        """Wait until the import is in one of `states` (default "ready")."""
        wanted = states or ("ready",)
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        current: dict[str, Any] = {}
        while time.monotonic() < deadline:
            current = self.import_state(name)
            if current.get("state") in wanted:
                return current
            time.sleep(0.25)
        raise LogosctlError(
            f"import {name!r} did not become {' or '.join(wanted)}; "
            f"last seen {current or 'nothing'}")

    def set_policy(self, policy: dict[str, list[str]]) -> None:
        """Replace the exporter's remote policy: {"<runtime id>/<consumer>":
        [target, …]}, "*" for any consumer or target. `{}` refuses every route."""
        path = self.exporter.config_dir / "remote-policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        self.exporter_client().peer("policy", "set", str(path))

    def served_routes(self) -> list[dict[str, Any]]:
        """The exporter's live routes: [{route, peer, consumer, target, expires_ms}]."""
        return self.exporter_client().peer("routes").get("served", [])

    # ── Internal ────────────────────────────────────────────────────────────

    def _start_exporter(self) -> None:
        assert self._root is not None
        daemon = LogosctlDaemon(
            self.modules_dir,
            binary=self.binary,
            config_dir=self._root / "exporter",
            extra_config={"peering": self.exporter_config()},
            startup_timeout=self.startup_timeout,
        )
        daemon.start()
        self._exporter = daemon
        # An export takes effect at the module's next load.
        client = daemon.client()
        for name in self.exports:
            client.load_module(name)
        deadline = time.monotonic() + self.timeout
        while True:
            shared = client.peer("exports")
            waiting = [n for n in self.exports if not shared.get(n, {}).get("port")]
            if not waiting:
                return
            if time.monotonic() > deadline:
                raise LogosctlError(
                    f"{', '.join(waiting)} loaded but never listened for peers; only a "
                    'plain module ("transport": "qt_remote_plain") can be exported')
            time.sleep(0.1)

    def _link(self) -> None:
        """Pair the importer with the exporter through the exporter's local
        invite, a single-use file that needs no code on either side."""
        invite = self.exporter.config_dir / "peering" / "local-invite"
        deadline = time.monotonic() + self.timeout
        while not invite.exists():
            if time.monotonic() > deadline:
                raise LogosctlError(f"the exporter wrote no local invite at {invite}")
            time.sleep(0.1)
        client = self.importer_client()
        started = client.peer("redeem", str(invite))
        pairing = started.get("id", "") if isinstance(started, dict) else ""
        while time.monotonic() < deadline:
            peers = client.peer("ls").get("peers", [])
            if any(p.get("runtime_id") == self.exporter_id for p in peers):
                return
            for p in client.peer("pending").get("pending", []):
                if p.get("id") == pairing and p.get("state") == "failed":
                    raise LogosctlError(f"pairing failed: {p.get('error') or p}")
            time.sleep(0.25)
        raise LogosctlError("the importer did not pair with the exporter in time")
