from __future__ import annotations

from arb_memory.mcp.config import mcp_connect


def readiness(*, conn_factory=None) -> dict:
    conn_factory = conn_factory or mcp_connect
    conn = None
    try:
        conn = conn_factory()
        conn.execute("SELECT count(*) FROM hints").fetchone()
        conn.execute("SELECT count(*) FROM mcp_auth.oauth_clients").fetchone()
    except Exception as exc:
        return {"ready": False, "degraded": True, "error": str(exc)}
    finally:
        if conn is not None and hasattr(conn, "close"):
            conn.close()
    return {"ready": True, "degraded": False}


def liveness() -> dict:
    return {"alive": True}
