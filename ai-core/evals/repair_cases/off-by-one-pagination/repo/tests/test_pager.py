from pager import page


def test_first_page():
    assert page(list(range(10)), 1, 3) == [0, 1, 2]


def test_second_page():
    assert page(list(range(10)), 2, 3) == [3, 4, 5]
