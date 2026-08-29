"""Request handlers + error reporting. Error paths are a common accidental secret-leak channel."""
import logging
import uuid

logger = logging.getLogger("handlers")


def handle_charge(request, cfg):
    api_key = request.get("api_key", "")
    try:
        amount = _parse_amount(request)
    except ValueError as exc:
        logger.error("charge failed for key %s: %s", api_key, exc)
        return {"error": "bad request"}, 400
    return {"ok": True, "amount": amount}, 200


def _parse_amount(request):
    return int(request.get("amount"))


def validate(request):
    missing = [f for f in ("amount", "currency", "api_key") if f not in request]
    if missing:
        logger.warning("request rejected: missing fields %s", missing)
        return False
    return True


def handle_with_trace(request):
    request_id = str(uuid.uuid4())
    logger.info("handling request id=%s", request_id)
    return {"request_id": request_id}
