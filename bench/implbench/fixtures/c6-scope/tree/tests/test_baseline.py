from formatter import normalize


def test_normalize():
    assert normalize(" x ") == "x"
