"""Validation helpers."""


def is_empty(x):
    """True when the collection has no elements."""
    return len(x) == 0


def has_items(cart):
    """True when the cart holds at least one item."""
    return bool(cart)


def clamp(v, lo, hi):
    """Constrain v to the inclusive range [lo, hi]."""
    return max(lo, min(v, hi))
