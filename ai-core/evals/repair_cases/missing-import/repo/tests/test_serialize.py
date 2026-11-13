from serialize import to_json


def test_sorted_keys():
    assert to_json({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
