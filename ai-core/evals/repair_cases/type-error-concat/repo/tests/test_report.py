from report import summary


def test_summary():
    assert summary(3) == "total: 3"
