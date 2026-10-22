from app import quota


def test_explicit_limit():
    assert quota({"limit": 50}) == 50


def test_free_plan_defaults_to_ten():
    assert quota({"name": "free"}) == 10
