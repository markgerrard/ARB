"""Small numeric utilities."""


def safe_div(a, b):
    """Divide a by b, returning 0.0 when b is zero. Float division throughout."""
    return a / b if b else 0.0
