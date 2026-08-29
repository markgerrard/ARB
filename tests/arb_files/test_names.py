import pytest

from arb_files.names import to_key, validate_name


@pytest.mark.parametrize("ok", ["report.pdf", "a/b/c.txt", "build-123_final.zip", "x"])
def test_valid_names_pass(ok):
    validate_name(ok)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/leading",
        "a//b",
        "../escape",
        "a/../b",
        "a/./b",
        "a/..",
        "..",
        ".",
        "with space.txt",
        "bad\\name",
        "a" * 257,
        "unié.txt",
        "a/",
        "tab\tname",
        ".trash/x",
    ],
)
def test_invalid_names_raise(bad):
    with pytest.raises(ValueError):
        validate_name(bad)


def test_to_key_prepends_prefix():
    assert to_key("agent-files/", "a/b.txt") == "agent-files/a/b.txt"


def test_to_key_validates_before_prepend():
    with pytest.raises(ValueError):
        to_key("agent-files/", "../mono-backup/x")


def test_to_key_rejects_recovery_namespace():
    with pytest.raises(ValueError):
        to_key("agent-files/", ".trash/anything")
