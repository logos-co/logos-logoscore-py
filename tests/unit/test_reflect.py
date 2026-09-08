"""Unit tests for the reflection helpers (pure — no daemon/binary needed)."""
import pytest

from logoscore._reflect import coerce, json_schema_type, ordered_args


def test_coerce_by_qt_type():
    assert coerce("100", "int") == 100
    assert coerce("1.5", "double") == 1.5
    assert coerce("true", "bool") is True
    assert coerce("off", "bool") is False
    assert coerce('{"a": 1}', "QVariantMap") == {"a": 1}
    assert coerce("hi", "QString") == "hi"
    assert coerce(7, "int") == 7          # already-typed passes through


def test_coerce_bad_number():
    with pytest.raises(ValueError):
        coerce("notanumber", "int")


def test_json_schema_type():
    assert json_schema_type("int") == {"type": "number"}
    assert json_schema_type("bool") == {"type": "boolean"}
    assert json_schema_type("QVariantMap") == {"type": "object"}
    assert json_schema_type("QString") == {"type": "string"}
    assert json_schema_type("") == {"type": "string"}


def test_ordered_args_prefix_and_unknown():
    params = [{"name": "a", "type": "QString"}, {"name": "b", "type": "int"}]
    assert ordered_args(params, {"a": "x", "b": "2"}) == ["x", 2]
    assert ordered_args(params, {"a": "x"}) == ["x"]     # trailing default omitted
    with pytest.raises(KeyError):
        ordered_args(params, {"zzz": "1"})
    with pytest.raises(ValueError):
        ordered_args(params, {"b": "2"})                 # gap before a provided arg
