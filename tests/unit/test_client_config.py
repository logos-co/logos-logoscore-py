"""Unit tests for the client-config writer / remote-connect API.

`LogoscoreClient.write_config` + `connect` centralize the on-disk
`client/config.json` schema that previously lived (hand-written) inside
the daemon helpers. These tests pin the serialized output byte-for-byte
(so the daemon paths keep emitting identical files) and the env-override
contract `connect()` relies on — none of which needs a real `logoscore`
binary or docker.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from logoscore import DaemonEndpoint, LogoscoreClient


def _dumped(obj: dict) -> str:
    """How every writer serializes config.json / auto.json."""
    return json.dumps(obj, indent=4) + "\n"


# ── write_config: serialization ──────────────────────────────────────────────


def test_write_config_tcp_minimal(tmp_path: Path):
    endpoints = {
        "core_service": DaemonEndpoint("tcp", "localhost", 8000, "json"),
        "capability_module": DaemonEndpoint("tcp", "localhost", 8001, "json"),
    }
    LogoscoreClient.write_config(tmp_path, endpoints)

    cfg_path = tmp_path / "client" / "config.json"
    expected = {
        "version": 2,
        "token_file": "auto.json",
        "daemon": {
            "core_service": {
                "transport": "tcp", "host": "localhost",
                "port": 8000, "codec": "json",
            },
            "capability_module": {
                "transport": "tcp", "host": "localhost",
                "port": 8001, "codec": "json",
            },
        },
    }
    assert cfg_path.read_text() == _dumped(expected)
    # No token → no auto.json.
    assert not (tmp_path / "client" / "auto.json").exists()


def test_write_config_tcp_ssl_emits_verify_peer_last(tmp_path: Path):
    endpoints = {
        "core_service": DaemonEndpoint(
            "tcp_ssl", "remote", 6000, "cbor", verify_peer=True),
    }
    LogoscoreClient.write_config(tmp_path, endpoints)

    block = json.loads((tmp_path / "client" / "config.json").read_text())[
        "daemon"]["core_service"]
    assert block == {
        "transport": "tcp_ssl", "host": "remote",
        "port": 6000, "codec": "cbor", "verify_peer": True,
    }
    # verify_peer is the final key (matches the old hand-written order).
    assert list(block)[-1] == "verify_peer"


def test_write_config_drops_verify_peer_for_plain_tcp(tmp_path: Path):
    # verify_peer only applies to tcp_ssl — a stray value on a tcp
    # endpoint must not leak into the config (matches old `extra={}`).
    endpoints = {"core_service": DaemonEndpoint("tcp", "h", 1, verify_peer=True)}
    LogoscoreClient.write_config(tmp_path, endpoints)
    block = json.loads((tmp_path / "client" / "config.json").read_text())[
        "daemon"]["core_service"]
    assert "verify_peer" not in block


def test_write_config_writes_token_file(tmp_path: Path):
    LogoscoreClient.write_config(
        tmp_path,
        {"core_service": DaemonEndpoint("tcp", "h", 1)},
        token="raw-secret",
    )
    auto = tmp_path / "client" / "auto.json"
    assert json.loads(auto.read_text()) == {"token": "raw-secret"}
    cfg = json.loads((tmp_path / "client" / "config.json").read_text())
    assert cfg["token_file"] == "auto.json"


def test_write_config_instance_id_present_when_set_even_if_empty(tmp_path: Path):
    LogoscoreClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)},
        instance_id="")
    cfg = json.loads((tmp_path / "client" / "config.json").read_text())
    assert cfg["instance_id"] == ""


def test_write_config_instance_id_absent_when_none(tmp_path: Path):
    LogoscoreClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)})
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
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 9)},
        merge=True)

    cfg = json.loads((client_dir / "config.json").read_text())
    assert cfg["keep_me"] == "yes"           # untouched pre-existing key
    assert cfg["version"] == 2
    assert cfg["daemon"] == {                 # daemon block fully replaced
        "core_service": {"transport": "tcp", "host": "h",
                         "port": 9, "codec": "json"},
    }


# ── Docker byte-for-byte regression ──────────────────────────────────────────
#
# `LogoscoreDockerDaemon._build_host_client_config` used to hand-write this
# exact dict. Pin that write_config reproduces it byte-for-byte so the
# refactor is inert on disk.


def _legacy_docker_cfg(transport_kind: str, *, verify_peer: bool | None) -> dict:
    extra = {"verify_peer": verify_peer} if transport_kind == "tcp_ssl" else {}
    return {
        "version": 2,
        "token_file": "auto.json",
        "instance_id": "abc123",
        "daemon": {
            "core_service": {
                "transport": transport_kind, "host": "localhost",
                "port": 7000, "codec": "json", **extra,
            },
            "capability_module": {
                "transport": transport_kind, "host": "localhost",
                "port": 7001, "codec": "json", **extra,
            },
        },
    }


@pytest.mark.parametrize("transport_kind,verify", [
    ("tcp", None),
    ("tcp_ssl", False),
    ("tcp_ssl", True),
])
def test_write_config_matches_legacy_docker_literal(
    tmp_path: Path, transport_kind: str, verify: bool | None,
):
    endpoints = {
        "core_service": DaemonEndpoint(
            transport_kind, "localhost", 7000, "json", verify),
        "capability_module": DaemonEndpoint(
            transport_kind, "localhost", 7001, "json", verify),
    }
    LogoscoreClient.write_config(
        tmp_path, endpoints, token="t", instance_id="abc123")

    got = (tmp_path / "client" / "config.json").read_text()
    assert got == _dumped(_legacy_docker_cfg(transport_kind, verify_peer=verify))


# ── connect(): env contract + temp-dir lifecycle ─────────────────────────────


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


def test_connect_sets_config_dir_and_no_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
):
    rec = _Recorder(monkeypatch)
    client = LogoscoreClient.connect(
        {
            "core_service": DaemonEndpoint("tcp", "remote", 6000),
            "capability_module": DaemonEndpoint("tcp", "remote", 6001),
        },
        token="tok",
    )
    client.status()

    env = rec.calls[0]["env"]
    assert env["LOGOSCORE_CONFIG_DIR"] == str(client.config_dir)
    for var in (
        "LOGOSCORE_CLIENT_TRANSPORT",
        "LOGOSCORE_CLIENT_TCP_HOST",
        "LOGOSCORE_CLIENT_TCP_PORT",
        "LOGOSCORE_CLIENT_NO_VERIFY_PEER",
        "LOGOSCORE_CLIENT_CODEC",
    ):
        assert var not in env, f"connect() leaked {var}"
    # Materialized the dial spec + token on disk.
    assert (client.config_dir / "client" / "config.json").exists()
    assert (client.config_dir / "client" / "auto.json").exists()


def test_connect_temp_dir_cleaned_up_by_finalizer(
    monkeypatch: pytest.MonkeyPatch,
):
    _Recorder(monkeypatch)
    client = LogoscoreClient.connect(
        {"core_service": DaemonEndpoint("tcp", "h", 1)})
    cfg_dir = client.config_dir
    assert cfg_dir.exists()
    assert client._config_dir_finalizer.alive

    client._config_dir_finalizer()  # simulate GC
    assert not cfg_dir.exists()


def test_connect_explicit_config_dir_is_not_owned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    client = LogoscoreClient.connect(
        {"core_service": DaemonEndpoint("tcp", "h", 1)},
        config_dir=tmp_path,
    )
    assert client.config_dir == tmp_path
    # Caller-supplied dir → no finalizer registered, dir survives.
    assert not hasattr(client, "_config_dir_finalizer")
    assert (tmp_path / "client" / "config.json").exists()


# ── LogoscoreDockerDaemon.client(): per-module ports, no env override ─────────
#
# The capability_module rides its OWN forwarded host port. A single
# LOGOSCORE_CLIENT_TCP_PORT env override is applied to every module
# uniformly by the CLI, so it would collapse capability_module onto
# core_service's port. These tests pin that client() is config-file-driven
# (distinct per-module ports, no transport env overrides).


def test_docker_client_keeps_distinct_capability_port_and_no_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from logoscore import LogoscoreDockerDaemon

    mods = tmp_path / "mods"
    mods.mkdir()
    rec = _Recorder(monkeypatch)
    d = LogoscoreDockerDaemon(image="img", modules_dir=mods)
    try:
        # Fake a started container with distinct forwarded ports.
        d._container_id = "fake"
        d._host_port = 7000
        d._host_cap_port = 7001
        client = d.client()

        cfg = json.loads(
            (d._host_client_dir / "client" / "config.json").read_text())
        assert cfg["daemon"]["core_service"]["port"] == 7000
        # The whole point: capability_module keeps 7001, NOT clobbered to 7000.
        assert cfg["daemon"]["capability_module"]["port"] == 7001

        client.status()
        env = rec.calls[0]["env"]
        for var in (
            "LOGOSCORE_CLIENT_TRANSPORT",
            "LOGOSCORE_CLIENT_TCP_HOST",
            "LOGOSCORE_CLIENT_TCP_PORT",
            "LOGOSCORE_CLIENT_CODEC",
            "LOGOSCORE_CLIENT_NO_VERIFY_PEER",
        ):
            assert var not in env, f"docker client() leaked {var}"
        assert env["LOGOSCORE_CONFIG_DIR"] == str(d._host_client_dir)
    finally:
        d._container_id = None
        d.stop()


def test_docker_client_tcp_host_baked_into_both_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from logoscore import LogoscoreDockerDaemon

    mods = tmp_path / "mods"
    mods.mkdir()
    _Recorder(monkeypatch)
    d = LogoscoreDockerDaemon(image="img", modules_dir=mods)
    try:
        d._container_id = "fake"
        d._host_port = 7000
        d._host_cap_port = 7001
        d.client(tcp_host="remote.example.com")
        daemon = json.loads(
            (d._host_client_dir / "client" / "config.json").read_text())["daemon"]
        assert daemon["core_service"]["host"] == "remote.example.com"
        assert daemon["capability_module"]["host"] == "remote.example.com"
    finally:
        d._container_id = None
        d.stop()


def test_docker_client_tcp_ssl_verify_peer_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from logoscore import LogoscoreDockerDaemon

    mods = tmp_path / "mods"
    mods.mkdir()
    cert = tmp_path / "c.pem"
    cert.write_text("x")
    key = tmp_path / "k.pem"
    key.write_text("y")
    _Recorder(monkeypatch)
    d = LogoscoreDockerDaemon(
        image="img", modules_dir=mods, transport="tcp_ssl",
        ssl_cert=cert, ssl_key=key, verify_peer=True)
    try:
        d._container_id = "fake"
        d._host_port = 7000
        d._host_cap_port = 7001

        # Default no_verify_peer=None → skip verification on disk.
        d.client()
        cfg = json.loads(
            (d._host_client_dir / "client" / "config.json").read_text())
        assert cfg["daemon"]["core_service"]["verify_peer"] is False

        # no_verify_peer=False → honour the constructor's verify_peer (True).
        d.client(no_verify_peer=False)
        daemon = json.loads(
            (d._host_client_dir / "client" / "config.json").read_text())["daemon"]
        assert daemon["core_service"]["verify_peer"] is True
        assert daemon["capability_module"]["verify_peer"] is True
    finally:
        d._container_id = None
        d.stop()


# ── LogoscoreDaemon.client(): config-driven, no port clobber ─────────────────
#
# Same lock-down as the docker daemon, applied to the local daemon: dial
# overrides are merged into the per-module client/config.json in place
# (preserving each module's own port + unmodeled fields) and the client
# carries NO LOGOSCORE_CLIENT_* env overrides.


def _seed_client_config(config_dir: Path, daemon_block: dict) -> None:
    client_dir = config_dir / "client"
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "config.json").write_text(_dumped({
        "version": 2, "token_file": "auto.json",
        "instance_id": "iid", "daemon": daemon_block,
    }))
    (client_dir / "auto.json").write_text(_dumped({"token": "t"}))


def _fake_local_daemon(tmp_path: Path, daemon_block: dict):
    from logoscore import LogoscoreDaemon

    _seed_client_config(tmp_path, daemon_block)
    d = LogoscoreDaemon(modules_dir=tmp_path / "mods", config_dir=tmp_path)
    d._process = object()  # bypass the "daemon not running" guard
    return d


def test_daemon_client_no_overrides_sets_no_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    rec = _Recorder(monkeypatch)
    d = _fake_local_daemon(tmp_path, {
        "core_service": {"transport": "tcp", "host": "127.0.0.1",
                         "port": 6000, "codec": "json"},
        "capability_module": {"transport": "tcp", "host": "127.0.0.1",
                              "port": 6001, "codec": "json"},
    })
    d.client().status()
    env = rec.calls[0]["env"]
    for var in (
        "LOGOSCORE_CLIENT_TRANSPORT",
        "LOGOSCORE_CLIENT_TCP_HOST",
        "LOGOSCORE_CLIENT_TCP_PORT",
        "LOGOSCORE_CLIENT_CODEC",
        "LOGOSCORE_CLIENT_NO_VERIFY_PEER",
    ):
        assert var not in env, f"daemon.client() leaked {var}"
    assert env["LOGOSCORE_CONFIG_DIR"] == str(tmp_path)
    assert env["LOGOSCORE_TOKEN"] == "t"


def test_daemon_client_overrides_preserve_distinct_ports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    d = _fake_local_daemon(tmp_path, {
        "core_service": {"transport": "tcp", "host": "127.0.0.1",
                         "port": 6000, "codec": "json"},
        "capability_module": {"transport": "tcp", "host": "127.0.0.1",
                              "port": 6001, "codec": "json"},
    })
    d.client(tcp_host="example.com", codec="cbor")
    daemon = json.loads(
        (tmp_path / "client" / "config.json").read_text())["daemon"]
    assert daemon["core_service"]["host"] == "example.com"
    assert daemon["capability_module"]["host"] == "example.com"
    assert daemon["core_service"]["codec"] == "cbor"
    assert daemon["capability_module"]["codec"] == "cbor"
    # The fix: capability_module keeps its OWN port (not clobbered to 6000).
    assert daemon["core_service"]["port"] == 6000
    assert daemon["capability_module"]["port"] == 6001


def test_daemon_client_preserves_tcp_ssl_ca_and_flips_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    d = _fake_local_daemon(tmp_path, {
        "core_service": {"transport": "tcp_ssl", "host": "127.0.0.1",
                         "port": 6000, "codec": "json",
                         "ca": "/ca.pem", "verify_peer": True},
        "capability_module": {"transport": "tcp_ssl", "host": "127.0.0.1",
                              "port": 6001, "codec": "json",
                              "ca": "/ca.pem", "verify_peer": True},
    })
    d.client(no_verify_peer=True)
    daemon = json.loads(
        (tmp_path / "client" / "config.json").read_text())["daemon"]
    for mod in ("core_service", "capability_module"):
        assert daemon[mod]["verify_peer"] is False
        # `ca` is not modeled by DaemonEndpoint — in-place edit keeps it.
        assert daemon[mod]["ca"] == "/ca.pem"
    assert daemon["core_service"]["port"] == 6000
    assert daemon["capability_module"]["port"] == 6001


def test_daemon_client_local_override_keeps_minimal_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    d = _fake_local_daemon(tmp_path, {
        "core_service": {"transport": "local"},
        "capability_module": {"transport": "local"},
    })
    d.client(transport="local")  # what the local integration suite passes
    daemon = json.loads(
        (tmp_path / "client" / "config.json").read_text())["daemon"]
    # No host/port/codec injected into a local entry.
    assert daemon["core_service"] == {"transport": "local"}
    assert daemon["capability_module"] == {"transport": "local"}


def test_daemon_client_rejects_tcp_port_kwarg(tmp_path: Path):
    d = _fake_local_daemon(tmp_path, {
        "core_service": {"transport": "tcp", "host": "h", "port": 1,
                         "codec": "json"},
    })
    # tcp_port is gone: it could only clobber capability_module's port.
    with pytest.raises(TypeError):
        d.client(tcp_port=6000)


def test_daemon_client_transport_to_local_strips_network_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _Recorder(monkeypatch)
    d = _fake_local_daemon(tmp_path, {
        "core_service": {"transport": "tcp", "host": "127.0.0.1",
                         "port": 6000, "codec": "json"},
        "capability_module": {"transport": "tcp", "host": "127.0.0.1",
                              "port": 6001, "codec": "json"},
    })
    d.client(transport="local")
    daemon = json.loads(
        (tmp_path / "client" / "config.json").read_text())["daemon"]
    # Stale host/port/codec dropped — minimal local shape.
    assert daemon["core_service"] == {"transport": "local"}
    assert daemon["capability_module"] == {"transport": "local"}


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
    LogoscoreClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)},
        token="tok", merge=True)

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
    LogoscoreClient.write_config(
        tmp_path, {"core_service": DaemonEndpoint("tcp", "h", 1)},
        token="tok", merge=True)

    cfg = json.loads((client_dir / "config.json").read_text())
    # Unsafe token_file is rejected → falls back to auto.json.
    assert cfg["token_file"] == "auto.json"
    assert json.loads((client_dir / "auto.json").read_text()) == {"token": "tok"}


# ── docker daemon: fail-fast guards ──────────────────────────────────────────


def test_client_endpoints_raises_before_start(tmp_path: Path):
    from logoscore import LogoscoreDockerDaemon, LogoscoreError

    mods = tmp_path / "mods"
    mods.mkdir()
    d = LogoscoreDockerDaemon(image="img", modules_dir=mods)
    # Ports are assigned in start(); building endpoints before that must
    # fail loudly rather than emit a portless config.
    with pytest.raises(LogoscoreError):
        d._client_endpoints("localhost", "json", None)


def test_build_host_client_config_raises_when_token_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from logoscore import LogoscoreDockerDaemon, LogoscoreError

    mods = tmp_path / "mods"
    mods.mkdir()
    d = LogoscoreDockerDaemon(image="img", modules_dir=mods, startup_timeout=0.1)
    try:
        d._container_id = "fake"
        d._host_port = 7000
        d._host_cap_port = 7001
        # Token never readable → fail fast instead of writing a config that
        # references a missing auto.json.
        monkeypatch.setattr(d, "read_container_file", lambda _p: None)
        with pytest.raises(LogoscoreError):
            d._build_host_client_config()
    finally:
        d._container_id = None
        # _host_client_dir is a real tmpdir; clean it up.
        shutil.rmtree(d._host_client_dir, ignore_errors=True)
        shutil.rmtree(d._config_dir, ignore_errors=True)
        shutil.rmtree(d._persistence_dir, ignore_errors=True)
