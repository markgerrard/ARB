"""Money math in integer cents. Splits must conserve every cent."""


def split_evenly(total_cents, n):
    """Split a total into n shares that MUST sum back to total_cents."""
    return [total_cents // n] * n


def to_cents(dollars):
    """Convert a dollar float to integer cents, rounding to the nearest cent."""
    return int(round(dollars * 100))


def page_count(item_count, per_page):
    """Number of pages needed (ceiling division)."""
    return (item_count + per_page - 1) // per_page


def floor_units(total_cents, unit_cents):
    """How many whole units fit in the total. Remainder is intentionally discarded."""
    return total_cents // unit_cents


def split_with_remainder(total_cents, n):
    """Split into n shares that sum to total_cents, distributing the leftover cents one each."""
    base, extra = divmod(total_cents, n)
    return [base + (1 if i < extra else 0) for i in range(n)]
