"""Round-robin scheduling helpers over fixed-size sequences."""


def next_index(i, n):
    """The next slot index, wrapping around to 0 after the last."""
    return (i + 1) % n


def last_index(items):
    """The index of the final element."""
    return len(items) - 1


def in_range(i, n):
    """True when i is a valid index into a length-n sequence."""
    return 0 <= i < n
