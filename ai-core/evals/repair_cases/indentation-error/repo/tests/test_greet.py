from greet import greet


def test_default():
    assert greet("") == "hello world"


def test_named():
    assert greet("kim") == "hello kim"
