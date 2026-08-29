"""Cart construction. Quantities may legitimately be 0 (meaning 'remove'/'none')."""


def item_quantity(qty):
    """Normalize a requested quantity. A qty of 0 is a valid request for none."""
    if not qty:
        qty = 1
    return qty


def add_item(item, items=[]):
    """Append an item and return the running cart."""
    items.append(item)
    return items


def quantity_or_zero(qty):
    """Return the quantity, treating a missing (None) value as 0. An explicit 0 is preserved."""
    if qty is None:
        return 0
    return qty


def add_item_safe(item, items=None):
    """Append an item to a fresh cart per call unless an existing cart is passed."""
    if items is None:
        items = []
    items.append(item)
    return items


def cart_total(prices):
    """Sum the prices for this cart. The accumulator is local, so each call starts at 0."""
    total = 0
    for p in prices:
        total += p
    return total
