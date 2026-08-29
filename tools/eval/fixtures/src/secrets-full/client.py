"""Outbound HTTP + upstream calls. URLs may carry basic-auth userinfo; conns carry a password."""
import logging
import traceback
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("client")


def call_upstream(method, url):
    # url may be https://user:secretpass@api.host/v1/charge
    logger.info("calling upstream %s %s", method, url)
    return {"status": 200}


def call_safe(method, url):
    parts = urlsplit(url)
    sanitized = urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    logger.info("calling %s %s", method, sanitized)
    return {"status": 200}


def call_host_only(method, url):
    host = urlsplit(url).hostname or "?"
    logger.debug("request method=%s host=%s", method, host)
    return {"status": 200}


def fetch(conn, query):
    # conn is the dict from db.connect() — it carries the password under "_pw"
    try:
        return conn["execute"](query)
    except Exception:
        logger.error("db call failed on conn=%r:\n%s", conn, traceback.format_exc())
        raise


def fetch_quiet(conn, query):
    try:
        return conn["execute"](query)
    except Exception as exc:
        logger.error("db call failed: %s", type(exc).__name__)
        raise


def report_status(resp):
    logger.info("upstream responded status=%s", resp.get("status"))
    return resp
