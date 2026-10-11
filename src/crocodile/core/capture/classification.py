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
    "token",
    "jwt",
    "api_key",
    "api-key",
    "api-sign",
    "auth",
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
    if contains_credentials(msg):
        return "control"
    if source in {"coinbase", "bybit"} and (
        frame.frame_kind == "binary" or not isinstance(msg, dict)
    ):
        return "unknown"
    if source == "bitfinex":
        return "market" if isinstance(msg, list) else "control"
    if source == "kraken":
        return (
            "market"
            if (
                isinstance(msg, dict)
                and msg.get("channel") in {"book", "level3", "trade", "ticker"}
                and msg.get("type") in {"snapshot", "update"}
            )
            else "control"
        )
    if source == "dydx":
        return (
            "market"
            if (
                isinstance(msg, dict)
                and msg.get("channel") in {"v4_orderbook", "v4_trades"}
                and msg.get("type") in {"subscribed", "channel_data", "channel_batch_data"}
            )
            else "control"
        )
    if source == "coinbase" and isinstance(msg, dict) and "channel" in msg:
        return (
            "market"
            if msg.get("channel")
            in {"l2_data", "heartbeats", "ticker", "market_trades", "subscriptions"}
            else "control"
        )
    if source == "bybit" and isinstance(msg, dict) and "op" in msg:
        return "control"
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


def public_control_data(source: str, msg: object) -> dict[str, str | int] | None:
    """Construct only public channel mapping fields, never copy a response object."""
    if source != "bitfinex" or not isinstance(msg, dict) or contains_credentials(msg):
        return None
    if msg.get("event") != "subscribed" or msg.get("channel") != "book":
        return None
    if not isinstance(msg.get("chanId"), int) or isinstance(msg["chanId"], bool):
        return None
    if not isinstance(msg.get("symbol"), str) or not msg["symbol"].startswith("t"):
        return None
    if msg.get("prec") != "R0" or msg.get("freq") != "F0":
        return None
    length = str(msg.get("len"))
    if length not in {"1", "25", "100", "250"}:
        return None
    return {
        "channel": "book",
        "chanId": msg["chanId"],
        "symbol": msg["symbol"],
        "prec": "R0",
        "freq": "F0",
        "len": length,
    }
