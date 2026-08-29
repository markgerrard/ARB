import pytest
from wordwrap import wrap


def test_basic():
    assert wrap("the quick brown fox", 9) == ["the quick", "brown fox"]


def test_single():
    assert wrap("hello", 10) == ["hello"]


def test_invalid_width():
    with pytest.raises(ValueError):
        wrap("x", 0)
