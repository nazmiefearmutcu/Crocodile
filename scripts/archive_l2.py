"""Explicit free OKX archive listing/download/source-time audit. No implicit large transfer."""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from crocodile.crypto.history import (
    ArchiveRequest,
    download_archive,
    iter_okx_archive,
    list_okx_archives,
    replay_history,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("--product", default="SPOT")
    parser.add_argument("--module", type=int, choices=[4, 5], default=4)
    parser.add_argument("--family")
    parser.add_argument("--aggregation", choices=["daily", "monthly"], default="daily")
    parser.add_argument(
        "--download-index", type=int, help="Download only this zero-based listed object"
    )
    parser.add_argument("--max-bytes", type=int, default=256 << 20)
    parser.add_argument("--directory", type=Path, default=Path("data/archives"))
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    listing = await list_okx_archives(
        ArchiveRequest(
            args.symbol,
            args.product,
            args.module,
            date.fromisoformat(args.start),
            date.fromisoformat(args.end),
            args.aggregation,
            args.family,
        )
    )
    result = asdict(listing)
    for obj in result["objects"]:
        # Ephemeral signed query credentials do not belong in reports.
        from crocodile.crypto.history.schema import public_url_identity

        obj["url"] = public_url_identity(obj["url"])
    if args.download_index is not None:
        if not 0 <= args.download_index < len(listing.objects):
            parser.error("download-index is outside the current catalogue")
        selected = listing.objects[args.download_index]
        manifest = await download_archive(selected, args.directory, max_bytes=args.max_bytes)
        result["download"] = asdict(manifest)
        if args.audit:
            result["audit"] = asdict(
                replay_history(iter_okx_archive(args.directory / selected.filename, manifest))
            )
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
