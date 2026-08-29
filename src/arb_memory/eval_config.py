"""Eval bus config. Producer (bridge) and consumer (ARB) MUST read the same env var names."""
import os

EVAL_STREAM_BASE = "eval:events"
EVAL_STREAM = EVAL_STREAM_BASE          # convenience alias (prefix applied by eval_stream())
EVAL_GROUP = "arbmem-eval"
DEFAULT_EVAL_DB = 4                      # db3 = memory/audit; db4 = eval; NOT db12 live, NOT db15 tests


def eval_prefix():
    return os.environ.get("ARB_EVAL_PREFIX", "")


def eval_stream():
    return f"{eval_prefix()}{EVAL_STREAM_BASE}"


def eval_redis_url():
    return os.environ.get("ARB_EVAL_REDIS_URL", "")


def eval_redis_db():
    return int(os.environ.get("ARB_EVAL_REDIS_DB", DEFAULT_EVAL_DB))
