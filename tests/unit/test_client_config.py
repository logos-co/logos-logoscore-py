"""Unit tests for the client-config writer / connect API.

`LogoscoreClient.write_config` + `connect` own the on-disk
`client/config.json` schema. These tests pin the serialized output
byte-for-byte and what `connect()` / `LogoscoreDaemon.client()` hand the
CLI — none of which needs a real `logoscore` binary.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from logoscore import DaemonEndpoint, LogoscoreClient

LOCAL = {"core_service": DaemonEndpoint(), "capability_module": DaemonEndpoint()}


def _dumped(obj: dict) -> str:
    """How every writer serializes config.json / auto.json."""
    return json.dumps(obj, indent=4) + "\n"


# ── write_config: serialization ──────────────────────────────────────────────


def test_write_config_local_minimal(tmp_path: Path):
    LogoscoreClient.write_config(tmp_path, LOCAL)

    cfg_path = tmp_path / "client" / "config.json"
    expected = {
        "version": 2,
        "token_file": "auto.json",
        "daemon": {
            "core_service": {"transport": "local"},
            "capability_module": {"transport": "local"},
        },
    }
    assert cfg_path.read_text() == _dumped(expected)
    # No token → no auto.json.
    assert not (tmp_path / "client" / "auto.json").exists()


def test_write_config_writes_token_file(tmp_path: Path):
    LogoscoreClient.write_config(tmp_path, LOCAL, token="raw-secret")
    auto = tmp_path / "client" / "auto.json"
    assert json.loads(auto.read_text()) == {"token": "raw-secret"}
    cfg = json.loads((tmp_path / "client" / "config.json").read_text())
    assert cfg["token_file"] == "auto.json"


def test_write_config_instance_id_present_when_set_even_if_empty(tmp_path: Path):
    LogoscoreClient.write_config(tmp_path, LOCAL, instance_id="")
    cfg = json.loads((tmp_path / "client" / "config.json").read_text())
    assert cfg["instance_id"] == ""


def test_write_config_instance_id_absent_when_none(tmp_path: Path):
    LogoscoreClient.write_config(tmp_path, LOCAL)
    cfg = json.loads((tmp_path / "client" / "config.json").read_text())
    assert "instance_id" not in cfg


def test_write_config_merge_preserves_existing_keys(tmp_path: Path):
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.json").write_text(_dumped({
        "version": 2,
        "token_file": "auto.json",
        "keep_me": "yes",
        "daemon": {"old": {"transport": "local"}},
    }))

    LogoscoreClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint()}, merge=True)

    cfg = json.loads((client_dir / "config.json").read_text())
    assert cfg["keep_me"] == "yes"           # untouched pre-existing key
    assert cfg["version"] == 2
    assert cfg["daemon"] == {"core_service": {"transport": "local"}}


# ── write_config: token_file consistency (merge) ─────────────────────────────


def test_write_config_token_honors_existing_token_file(tmp_path: Path):
    # A merged config with a custom token_file must get the token written
    # to THAT file, not a hardcoded auto.json — else config + token diverge.
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.json").write_text(_dumped({
        "version": 2, "token_file": "custom.json",
        "daemon": {"core_service": {"transport": "local"}},
    }))
    LogoscoreClient.write_config(tmp_path, LOCAL, token="tok", merge=True)

    cfg = json.loads((client_dir / "config.json").read_text())
    assert cfg["token_file"] == "custom.json"
    assert json.loads((client_dir / "custom.json").read_text()) == {"token": "tok"}
    assert not (client_dir / "auto.json").exists()


@pytest.mark.parametrize("bad", ["../evil.json", "sub/dir.json", "/abs.json"])
def test_write_config_token_file_traversal_falls_back(tmp_path: Path, bad: str):
    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "config.json").write_text(_dumped({
        "version": 2, "token_file": bad,
        "daemon": {"core_service": {"transport": "local"}},
    }))
    LogoscoreClient.write_config(tmp_path, LOCAL, token="tok", merge=True)

    cfg = json.loads((client_dir / "config.json").read_text())
    # Unsafe token_file is rejected → falls back to auto.json.
    assert cfg["token_file"] == "auto.json"
    assert json.loads((client_dir / "auto.json").read_text()) == {"token": "tok"}


# ── connect(): the dial spec is on disk, plus temp-dir lifecycle ─────────────


class _Recorder:
    """Captures subprocess.run calls; returns an empty-JSON success."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(subprocess, "run", self._run)

    def _run(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})

        class _P:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return _P()


def test_connect_materializes_the_spec_and_selects_its_dir(
    monkeypatch: pytest.MonkeyPatch,
):
    rec = _Recorder(monkeypatch)
    client = LogoscoreClient.connect(LOCAL, token="tok", instance_id="iid")
    client.status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCORE_CONFIG_DIR"] == str(client.config_dir)
    cfg = json.loads((client.config_dir / "client" / "config.json").read_text())
    assert cfg["instance_id"] == "iid"
    assert (client.config_dir / "client" / "auto.json").exists()


def test_connect_temp_dir_cleaned_up_by_finalizer(
    monkeypatch: pytest.MonkeyPatch,
):
    _Recorder(monkeypatch)
    client = LogoscoreClient.connect(LOCAL)
    cfg_dir = client.config_dir
    assert cfg_dir.exists()
    assert client._config_dir_finalizer.alive

    client._config_dir_finalizer()  # simulate GC
    assert not cfg_dir.exists()


def test_connect_explicit_config_dir_is_not_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    client = LogoscoreClient.connect(LOCAL, config_dir=tmp_path)
    assert client.config_dir == tmp_path
    # Caller-supplied dir → no finalizer registered, dir survives.
    assert not hasattr(client, "_config_dir_finalizer")
    assert (tmp_path / "client" / "config.json").exists()


# ── LogoscoreDaemon.client(): the daemon's own spec and boot token ───────────


def test_daemon_client_carries_config_dir_and_boot_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from logoscore import LogoscoreDaemon

    client_dir = tmp_path / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "auto.json").write_text(_dumped({"token": "t"}))
    rec = _Recorder(monkeypatch)
    d = LogoscoreDaemon(modules_dir=tmp_path / "mods", config_dir=tmp_path)
    d._process = object()  # bypass the "daemon not running" guard
    d.client().status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCORE_CONFIG_DIR"] == str(tmp_path)
    assert env["LOGOSCORE_TOKEN"] == "t"
    assert {k for k, v in env.items() if os.environ.get(k) != v} == {
        "LOGOSCORE_CONFIG_DIR", "LOGOSCORE_TOKEN"}
    # The daemon's own file is left alone.
    assert not (client_dir / "config.json").exists()
