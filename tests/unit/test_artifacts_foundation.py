from snagentic.artifacts import (
    canonical_json,
    content_hash,
    normalize_line_endings,
    normalize_mapping,
)


def test_normalization_is_deterministic_without_destroying_list_order() -> None:
    left = {"z": None, "a": {"b": "one\r\ntwo", "a": [2, 1]}}
    right = {"a": {"a": [2, 1], "b": "one\ntwo"}, "z": None}
    assert normalize_mapping(left) == normalize_mapping(right)
    assert canonical_json(left) == canonical_json(right)
    assert content_hash(left) == content_hash(right)


def test_normalization_can_omit_mapping_nulls_and_normalizes_text() -> None:
    assert canonical_json({"b": None, "a": [None]}, omit_nulls=True) == '{"a":[null]}'
    assert normalize_line_endings("one\r\ntwo\r") == "one\ntwo\n"
