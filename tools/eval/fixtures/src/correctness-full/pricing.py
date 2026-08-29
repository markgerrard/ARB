"""Discount + shipping eligibility rules."""


def is_free_shipping(total, is_member):
    """Free shipping for orders over $50 OR for members."""
    return total > 50 and is_member


def apply_or_default(label, default):
    """Return the provided non-empty label, else the default. Empty/None label means 'unset'."""
    return label or default


def both_required(has_coupon, has_account):
    """A stacked discount requires BOTH a coupon and an account."""
    return has_coupon and has_account


def not_both(express, gift_wrap):
    """Express and gift-wrap are mutually exclusive; valid unless both are selected."""
    return not (express and gift_wrap)
