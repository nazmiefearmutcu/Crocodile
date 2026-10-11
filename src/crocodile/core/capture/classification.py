"""Classify mixed sockets without persisting authentication payloads."""

from typing import Literal

from crocodile.core.capture.schema import RawFrame

FrameClass = Literal["market", "control", "unknown"]
_SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "authorization",
    "signature",
}


def contains_credentials(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(k).lower() in _SECRET_KEYS or contains_credentials(v) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(contains_credentials(v) for v in value)
    return False


def classify_frame(source: str, frame: RawFrame, msg: object | None) -> FrameClass:
    if source == "deribit":
        if not isinstance(msg, dict):
            return "unknown"
        params = msg.get("params")
        if (
            msg.get("method") == "subscription"
            and isinstance(params, dict)
            and isinstance(params.get("channel"), str)
            and params["channel"].startswith(("book.", "trades.", "ticker."))
            and isinstance(params.get("data"), (dict, list))
            and not contains_credentials(msg)
        ):
            return "market"
        return "control"
    if isinstance(msg, dict) and (
        "id" in msg or "error" in msg or msg.get("e") == "serverShutdown"
    ):
        return "control"
    return "market"
