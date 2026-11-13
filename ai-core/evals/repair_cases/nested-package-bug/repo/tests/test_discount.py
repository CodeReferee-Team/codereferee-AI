from shop.pricing.discount import discounted


def test_threshold_is_inclusive():
    assert discounted(100) == 90.0


def test_below_threshold():
    assert discounted(99) == 99
