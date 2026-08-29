import os
import threading

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

_OPENAI_CLIENT = None
_OPENAI_CLIENT_API_KEY = None
_OPENAI_CLIENT_LOCK = threading.Lock()


def _openai_client():
    global _OPENAI_CLIENT, _OPENAI_CLIENT_API_KEY
    api_key = os.environ.get("OPENAI_API_KEY")
    if _OPENAI_CLIENT is not None and api_key == _OPENAI_CLIENT_API_KEY:
        return _OPENAI_CLIENT
    with _OPENAI_CLIENT_LOCK:
        if _OPENAI_CLIENT is None or api_key != _OPENAI_CLIENT_API_KEY:
            from openai import OpenAI

            _OPENAI_CLIENT = OpenAI(api_key=api_key)
            _OPENAI_CLIENT_API_KEY = api_key
    return _OPENAI_CLIENT


def embed(text: str) -> list[float]:
    response = _openai_client().embeddings.create(model=EMBED_MODEL, input=text)
    return list(response.data[0].embedding)
