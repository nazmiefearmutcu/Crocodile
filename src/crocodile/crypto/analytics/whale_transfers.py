from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from crocodile.core.errors import ConfigError

if TYPE_CHECKING:
    from web3 import AsyncWeb3


def filter_transfers_by_usd(
    transfers: Iterable[Mapping[str, Any]],
    usd_threshold: float,
) -> list[dict[str, Any]]:
    """Return decoded transfer dicts whose ``usd_value`` meets ``usd_threshold``.

    Pure helper for offline CSV/JSON pipelines (no RPC).
    """
    if usd_threshold < 0:
        raise ValueError("usd_threshold must be non-negative")

    out: list[dict[str, Any]] = []
    for row in transfers:
        raw = row.get("usd_value", row.get("amount", row.get("value")))
        if raw is None:
            continue
        try:
            usd_value = float(raw)
        except (TypeError, ValueError):
            continue
        if usd_value >= usd_threshold:
            item = dict(row)
            item["usd_value"] = usd_value
            out.append(item)
    return out


def label_transfer_addresses(
    transfers: Iterable[Mapping[str, Any]],
    watchlist: Mapping[str, str],
    *,
    from_key: str = "from",
    to_key: str = "to",
) -> list[dict[str, Any]]:
    """Annotate transfer rows with ``from_label`` / ``to_label`` from a watchlist.

    Watchlist keys are matched case-insensitively. Unknown addresses keep an
    empty label string so downstream tables stay columnar.
    """
    labels = {
        str(k).strip().lower(): str(v)
        for k, v in watchlist.items()
        if k is not None and str(k).strip()
    }
    out: list[dict[str, Any]] = []
    for row in transfers:
        item = dict(row)
        from_addr = item.get(from_key) or item.get("from_address") or item.get("sender")
        to_addr = item.get(to_key) or item.get("to_address") or item.get("recipient")
        # Blank / missing addresses never match a watchlist entry.
        from_s = str(from_addr).strip().lower() if from_addr is not None else ""
        to_s = str(to_addr).strip().lower() if to_addr is not None else ""
        item["from_label"] = labels.get(from_s, "") if from_s else ""
        item["to_label"] = labels.get(to_s, "") if to_s else ""
        item["is_known"] = bool(item["from_label"] or item["to_label"])
        out.append(item)
    return out


def label_known_addresses(
    addresses: Sequence[str],
    watchlist: Mapping[str, str],
) -> list[dict[str, str]]:
    """Map a list of addresses to watchlist labels (empty string if unknown)."""
    labels = {
        str(k).strip().lower(): str(v)
        for k, v in watchlist.items()
        if k is not None and str(k).strip()
    }
    rows: list[dict[str, str]] = []
    for addr in addresses:
        key = str(addr).strip().lower() if addr is not None else ""
        label = labels.get(key, "") if key else ""
        rows.append(
            {
                "address": str(addr),
                "label": label,
                "is_known": "true" if key and key in labels else "false",
            }
        )
    return rows


class WhaleTransferTracker:
    """Tracks large on-chain ERC-20 token transfers exceeding a USD threshold."""

    def __init__(
        self,
        w3: AsyncWeb3,
        token_address: str,
        token_price_usd: float,
        usd_threshold: float = 100000.0,
        decimals: int = 18,
    ) -> None:
        self.w3 = w3
        self.token_address = w3.to_checksum_address(token_address)
        self.token_price_usd = token_price_usd
        self.usd_threshold = usd_threshold
        self.decimals = decimals
        # keccak("Transfer(address,address,uint256)")
        self.transfer_topic = (
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )

    async def get_whale_transfers(
        self, start_block: int, end_block: int
    ) -> list[dict[str, Any]]:
        """Fetch and filter Transfer logs within block range for whale movements."""
        try:
            import eth_abi
        except ModuleNotFoundError as exc:
            if exc.name != "eth_abi":
                raise
            raise ConfigError(
                "On-chain transfer decoding requires: pip install 'crocodile[onchain]'"
            ) from exc
        try:
            logs = await self.w3.eth.get_logs(
                {
                    "address": self.token_address,
                    "topics": [self.transfer_topic],
                    "fromBlock": start_block,
                    "toBlock": end_block,
                }
            )
        except Exception:
            return []

        whales = []
        for log in logs:
            topics = log.get("topics", [])
            data = log.get("data", "")

            # Ensure we have a valid Transfer log: 3 topics [TransferTopic, from, to]
            if len(topics) < 3:
                continue

            try:
                # Decode addresses from topics
                from_addr = "0x" + topics[1].hex()[-40:]
                to_addr = "0x" + topics[2].hex()[-40:]
                from_addr_checksum = self.w3.to_checksum_address(from_addr)
                to_addr_checksum = self.w3.to_checksum_address(to_addr)

                # Decode amount from data
                data_hex = data.hex() if isinstance(data, bytes) else data
                data_hex = (
                    data_hex[2:] if data_hex.startswith("0x") else data_hex
                )
                data_bytes = bytes.fromhex(data_hex)

                raw_value = eth_abi.decode(["uint256"], data_bytes)[0]
                amount = float(raw_value) / (10**self.decimals)
                usd_value = amount * self.token_price_usd

                if usd_value >= self.usd_threshold:
                    whales.append({
                        "transaction_hash": log.get("transactionHash").hex()
                        if isinstance(log.get("transactionHash"), bytes)
                        else str(log.get("transactionHash", "")),
                        "block_number": int(log.get("blockNumber", 0)),
                        "from": from_addr_checksum,
                        "to": to_addr_checksum,
                        "amount": amount,
                        "usd_value": usd_value,
                    })
            except Exception:
                continue

        return whales
