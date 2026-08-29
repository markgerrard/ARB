import pytest

from arb_email.addresses import parse_single_recipient as P
from arb_email.addresses import recipient_allowed as A


@pytest.mark.parametrize(
    "ok",
    [
        "arb@example.com",
        "arb@example.com",
        "a.b+x@mail.example.com",
        " arb@example.com ",
    ],
)
def test_valid_single_address_normalises_lower(ok):
    out = P(ok)
    assert out == ok.strip().lower()


@pytest.mark.parametrize(
    "bad",
    [
        '"evil@x.com" <arb@example.com>',
        '"arb@example.com" <evil@x.com>',
        "a@f.com, b@evil.com",
        "arb@example.com\r\nBcc: v@x.com",
        "arb@example.com\tx",
        "arb@example.com (comment)",
        "mark(c)@example.com",
        "arb@example.com;evil@x.com",
        "arb@example.com evil@x.com",
        "@evil.com:arb@example.com",
        "arb@example.com\\@evil.com",
        "",
        "no-at",
        "a@@b",
        "a@b@c",
        "arb@example.com\x7f",
        "arb@example.com\x85",
        "arb@example.com\u2028",
        "arb@example.com\u2029",
    ],
)
def test_rejects_every_bypass(bad):
    with pytest.raises(ValueError):
        P(bad)


def test_allowlist_exact_address():
    assert A("arb@example.com", ["arb@example.com"]) is True
    assert A("evil@example.com", ["arb@example.com"]) is False


def test_allowlist_domain_exact_host_no_suffix_bug():
    al = ["@example.com"]
    assert A("x@example.com", al) is True
    assert A("x@evilexample.com", al) is False
    assert A("x@mail.example.com", al) is False
    assert A("x@example.com.evil.com", al) is False
    assert A("x@evil-example.com", al) is False
    assert A("x@exam\u0440le.com", al) is False


def test_recipient_allowed_failclosed_on_malformed():
    assert A("", ["@example.com"]) is False
    assert A("a@b@c", ["@example.com"]) is False

