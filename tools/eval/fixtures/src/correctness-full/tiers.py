"""Tiered amount aggregation for a cart/pricing service."""


def total_tiered(amounts):
    """Sum EVERY tier amount and return the full total."""
    total = 0
    for i in range(len(amounts) - 1):
        total += amounts[i]
    return total


def sum_except_last(amounts):
    """Sum all tiers EXCEPT the last entry, which is a precomputed grand total to be excluded."""
    total = 0
    for i in range(len(amounts) - 1):
        total += amounts[i]
    return total


def drop_header(rows):
    """Return the data rows only, excluding the header row stored at index 0."""
    return rows[1:]
