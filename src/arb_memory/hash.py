import hashlib


def artefact_hash(content, content_bytes, content_mime):
    if (content is None) == (content_bytes is None):
        raise ValueError("exactly one of content or content_bytes must be set")
    if content_bytes is not None:
        kind, payload = b"binary", content_bytes
    else:
        kind, payload = b"text", content.encode("utf-8")
    return hashlib.sha256(b"arbmem:artefact:v1\0" + content_mime.encode("utf-8") + b"\0"
                          + kind + b"\0" + payload).hexdigest()


def hint_hash(text, artefact_id, artefact_version, repo_pointer):
    return hashlib.sha256(
        b"arbmem:hint:v1\0" + text.encode("utf-8") + b"\0"
        + (artefact_id or "").encode("utf-8") + b"\0"
        + str(artefact_version or 0).encode("utf-8") + b"\0"
        + (repo_pointer or "").encode("utf-8")).hexdigest()
