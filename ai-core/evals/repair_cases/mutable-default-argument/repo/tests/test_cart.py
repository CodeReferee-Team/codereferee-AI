from cart import add_item


def test_new_carts_are_independent():
    assert add_item("apple") == ["apple"]
    assert add_item("pear") == ["pear"]
