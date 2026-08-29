import pytest

psycopg = pytest.importorskip("psycopg")

from arb_memory.hash import artefact_hash, hint_hash


def test_artefact_hash_mime_and_kind_change_the_hash():
    a = artefact_hash("hello", None, "text/plain")
    assert a != artefact_hash("hello", None, "text/markdown")
    assert a != artefact_hash(None, b"hello", "application/octet-stream")
    assert a == artefact_hash("hello", None, "text/plain")


@pytest.mark.parametrize(
    ("content", "content_bytes"),
    [
        ("hello", b"hello"),
        (None, None),
    ],
)
def test_artefact_hash_requires_exactly_one_content_form(content, content_bytes):
    with pytest.raises(ValueError):
        artefact_hash(content, content_bytes, "text/plain")


def test_hint_hash_is_version_aware():
    assert hint_hash("desc", "doc.md", 1, None) != hint_hash("desc", "doc.md", 2, None)
    assert hint_hash("desc", "doc.md", 1, None) == hint_hash("desc", "doc.md", 1, None)
    assert hint_hash("lead", None, None, None) == hint_hash("lead", None, None, None)


def test_hint_hash_exact_text_no_normalization():
    assert hint_hash("a b", None, None, None) != hint_hash("a  b", None, None, None)
