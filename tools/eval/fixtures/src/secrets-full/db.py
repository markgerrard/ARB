"""Data layer. Query logging must log shapes/metadata, never bound parameters or credentials."""
import logging

logger = logging.getLogger("db")


def run_query(conn, template, params):
    logger.debug("executing query: %s", template)
    return conn.execute(template, params)


def log_result(rows):
    logger.info("query returned %d rows", len(rows))
    return rows


def connect(host, port, password):
    logger.info("connecting to db at %s:%s", host, port)
    return {"host": host, "port": port, "_pw": password}
