"""The `peering:` sections PeeredDaemons gives its two daemons, checked
without a binary."""
from __future__ import annotations

import pytest

from logosctl import PeeredDaemons
from logosctl.daemon import _yaml_document


def test_the_exporter_grants_its_exports_through_the_local_invite():
    pair = PeeredDaemons("/modules", ["alpha", "beta"], events=False)
    config = pair.exporter_config()
    assert config["control"] == {"enabled": True, "host": "127.0.0.1", "port": 0,
                                 "local_invite": {"allow": ["*"]}}
    assert config["exports"] == {"enabled": True,
                                 "modules": {"alpha": {"events": False},
                                             "beta": {"events": False}}}


def test_the_importer_only_dials_out():
    assert "control" not in PeeredDaemons("/modules", ["alpha"]).importer_config()


def test_the_allow_list_survives_yaml():
    # A bare `*` would be a YAML alias, not the string.
    text = _yaml_document({"peering": PeeredDaemons("/m", ["alpha"]).exporter_config()})
    assert '- "*"' in text
    assert "port: 0" in text


def test_an_export_is_required():
    with pytest.raises(ValueError):
        PeeredDaemons("/modules", [])
