"""Whole-market census — a live, quantified snapshot of the entire crypto market.

This is the proof, not the claim.  :func:`market_census` concurrently measures
three axes of "the whole market" and folds them into one structured snapshot:

* **Venues** — enumerate tradable markets across the major ccxt exchanges,
  counted by kind (spot / perpetual / future / option).
* **Coins** — CoinGecko's global state: the full active-coin count, total market
  cap, 24 h volume, BTC/ETH dominance, and the top coins + 24 h movers.
* **DeFi** — DeFiLlama total value locked across every chain.

:func:`render_html` turns the snapshot into a self-contained, theme-aware
dashboard; :func:`render_terminal` prints a Rich summary.  All numbers are live
(keyless public APIs) — nothing here is synthetic.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import aiohttp

from crocodile.crypto.exchanges.coingecko.client import fetch_global, fetch_markets
from crocodile.crypto.exchanges.factory import list_ccxt_exchanges, list_exchanges
from crocodile.crypto.instruments.registry import Kind

log = logging.getLogger(__name__)

# A curated set of high-liquidity venues to deep-enumerate by default.  The
# census still *reports* the full reachable count; it only deep-counts these to
# stay inside sane time / rate-limit bounds.
DEFAULT_VENUES = [
    "binance", "bybit", "okx", "coinbase", "kraken", "kucoin",
    "mexc", "gate", "htx", "bitget", "bingx", "cryptocom",
]

_KIND_KEYS = ("spot", "perpetual", "future", "option")


async def _venue_census(exchange: str, sem: asyncio.Semaphore) -> dict[str, Any] | None:
    """Count one venue's markets by kind via ccxt ``load_markets``."""
    import ccxt.async_support as ccxt  # noqa: PLC0415

    async with sem:
        try:
            ex = getattr(ccxt, exchange)({"enableRateLimit": True})
        except Exception:
            return None
        counts = {k: 0 for k in _KIND_KEYS}
        try:
            markets = await ex.load_markets()
            from crocodile.crypto.exchanges.ccxt_universal import normalize as norm

            for m in markets.values():
                counts[norm.kind_from_market(m).value] += 1
            total = sum(counts.values())
        except Exception as exc:
            log.debug("census: %s enumerate failed: %s", exchange, exc)
            return None
        finally:
            await ex.close()
        return {"exchange": exchange, "markets": total, **counts}


async def market_census(
    *,
    generated_ns: int,
    venues: list[str] | None = None,
    coin_pages: int = 1,
    concurrency: int = 8,
) -> dict[str, Any]:
    """Measure the whole market concurrently and return a structured snapshot.

    ``generated_ns`` is passed in (the caller stamps the time — this module does
    not read the clock so it stays deterministic under test).
    """
    venue_ids = venues if venues is not None else DEFAULT_VENUES
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession() as session:
        venue_task = asyncio.gather(
            *(_venue_census(v, sem) for v in venue_ids), return_exceptions=True
        )
        global_task = fetch_global(session)
        markets_task = fetch_markets(session, pages=coin_pages)
        defi_task = _fetch_defi(session)

        venue_rows_raw, global_data, coins, defi = await asyncio.gather(
            venue_task, global_task, markets_task, defi_task
        )

    venue_rows = [
        r for r in venue_rows_raw if isinstance(r, dict) and r is not None
    ]
    venue_rows.sort(key=lambda r: r["markets"], reverse=True)
    total_markets = sum(r["markets"] for r in venue_rows)
    kind_totals = {k: sum(r[k] for r in venue_rows) for k in _KIND_KEYS}

    ccxt_ids = list_ccxt_exchanges()
    native = list_exchanges()

    return {
        "generated_ns": generated_ns,
        "connectors": {
            "native": sorted(native),
            "native_count": len(native),
            "ccxt_count": len(ccxt_ids),
            "total_reachable": len(set(native) | set(ccxt_ids)),
        },
        "venues": {
            "enumerated": len(venue_rows),
            "total_markets": total_markets,
            "by_kind": kind_totals,
            "rows": venue_rows,
        },
        "coins": _coin_summary(global_data, coins),
        "defi": defi,
    }


async def _fetch_defi(session: aiohttp.ClientSession) -> dict[str, Any]:
    try:
        timeout = aiohttp.ClientTimeout(total=20.0)
        async with session.get(
            "https://api.llama.fi/v2/chains", timeout=timeout
        ) as resp:
            resp.raise_for_status()
            chains = await resp.json()
    except Exception as exc:
        log.debug("census: defillama failed: %s", exc)
        return {"chains": 0, "total_tvl_usd": 0.0, "top_chains": []}
    rows: list[dict[str, Any]] = [
        {"name": c.get("name"), "tvl": float(c.get("tvl") or 0.0)}
        for c in chains
        if isinstance(c, dict)
    ]
    rows.sort(key=lambda r: float(r["tvl"]), reverse=True)
    return {
        "chains": len(rows),
        "total_tvl_usd": sum(float(r["tvl"]) for r in rows),
        "top_chains": rows[:8],
    }


def _coin_summary(global_data: dict[str, Any], coins: list[dict[str, Any]]) -> dict[str, Any]:
    mcap = (global_data.get("total_market_cap") or {}).get("usd", 0.0)
    vol = (global_data.get("total_volume") or {}).get("usd", 0.0)
    dom = global_data.get("market_cap_percentage") or {}

    def _slim(c: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": str(c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price": c.get("current_price"),
            "mcap": c.get("market_cap"),
            "rank": c.get("market_cap_rank"),
            "change_24h": c.get("price_change_percentage_24h"),
        }

    priced = [c for c in coins if c.get("price_change_percentage_24h") is not None]
    gainers = sorted(priced, key=lambda c: c["price_change_percentage_24h"], reverse=True)
    losers = sorted(priced, key=lambda c: c["price_change_percentage_24h"])
    return {
        "active": global_data.get("active_cryptocurrencies", 0),
        "tracked_markets": global_data.get("markets", 0),
        "total_mcap_usd": float(mcap or 0.0),
        "total_vol_24h_usd": float(vol or 0.0),
        "btc_dominance": float(dom.get("btc", 0.0) or 0.0),
        "eth_dominance": float(dom.get("eth", 0.0) or 0.0),
        "sampled": len(coins),
        "top": [_slim(c) for c in coins[:10]],
        "top_gainers": [_slim(c) for c in gainers[:5]],
        "top_losers": [_slim(c) for c in losers[:5]],
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _fmt_usd(v: float) -> str:
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.0f}"


def render_terminal(census: dict[str, Any]) -> str:
    """Plain-text summary (the CLI wraps this in Rich panels)."""
    c = census
    lines: list[str] = []
    conn = c["connectors"]
    lines.append(
        f"Reachable venues: {conn['total_reachable']} "
        f"({conn['native_count']} native + {conn['ccxt_count']} ccxt)"
    )
    v = c["venues"]
    bk = v["by_kind"]
    lines.append(
        f"Markets across {v['enumerated']} majors: {v['total_markets']:,} "
        f"(spot {bk['spot']:,} · perp {bk['perpetual']:,} · "
        f"future {bk['future']:,} · option {bk['option']:,})"
    )
    co = c["coins"]
    lines.append(
        f"Coin universe: {co['active']:,} active · mcap {_fmt_usd(co['total_mcap_usd'])} "
        f"· 24h vol {_fmt_usd(co['total_vol_24h_usd'])} · BTC dom {co['btc_dominance']:.1f}%"
    )
    d = c["defi"]
    lines.append(f"DeFi TVL: {_fmt_usd(d['total_tvl_usd'])} across {d['chains']} chains")
    return "\n".join(lines)


def render_html(census: dict[str, Any], *, generated_iso: str = "") -> str:
    """Self-contained, theme-aware 'Market Census' dashboard.

    Uses ``str.replace`` substitution (not ``str.format``) so the CSS can keep
    natural single braces.
    """
    c = census
    conn, v, co, d = c["connectors"], c["venues"], c["coins"], c["defi"]
    bk = v["by_kind"]
    e = html.escape

    max_markets = max((r["markets"] for r in v["rows"]), default=1)
    venue_bars = "".join(
        f'<div class="bar-row"><span class="bar-name">{e(r["exchange"])}</span>'
        f'<span class="bar-track"><span class="bar-fill" style="width:{max(2, r["markets"] * 100 // max_markets)}%"></span></span>'
        f'<span class="bar-val">{r["markets"]:,}</span></div>'
        for r in v["rows"]
    )

    def coin_rows(items: list[dict[str, Any]], show_change: bool = True) -> str:
        out = []
        for it in items:
            price = it.get("price")
            price_s = _fmt_usd(price) if isinstance(price, (int, float)) else "—"
            ch = it.get("change_24h")
            if show_change and isinstance(ch, (int, float)):
                cls = "up" if ch >= 0 else "down"
                ch_s = f'<span class="chg {cls}">{ch:+.1f}%</span>'
            else:
                ch_s = ""
            rank = it.get("rank")
            rank_s = f"#{rank}" if rank else ""
            out.append(
                f'<tr><td class="rank">{e(str(rank_s))}</td>'
                f'<td class="sym">{e(str(it.get("symbol") or ""))}</td>'
                f'<td class="nm">{e(str(it.get("name") or ""))}</td>'
                f'<td class="px">{e(price_s)}</td><td class="ch">{ch_s}</td></tr>'
            )
        return "".join(out)

    defi_rows = "".join(
        f'<div class="bar-row"><span class="bar-name">{e(str(ch["name"]))}</span>'
        f'<span class="bar-val">{_fmt_usd(ch["tvl"])}</span></div>'
        for ch in d.get("top_chains", [])
    )

    btc_dom = co["btc_dominance"]
    eth_dom = co["eth_dominance"]
    other_dom = max(0.0, 100.0 - btc_dom - eth_dom)

    fields: dict[str, str] = {
        "generated": e(generated_iso),
        "reachable": str(conn["total_reachable"]),
        "native": str(conn["native_count"]),
        "ccxt": str(conn["ccxt_count"]),
        "total_markets": f"{v['total_markets']:,}",
        "majors": str(v["enumerated"]),
        "spot": f"{bk['spot']:,}",
        "perp": f"{bk['perpetual']:,}",
        "future": f"{bk['future']:,}",
        "option": f"{bk['option']:,}",
        "active_coins": f"{co['active']:,}",
        "mcap": _fmt_usd(co["total_mcap_usd"]),
        "vol": _fmt_usd(co["total_vol_24h_usd"]),
        "btc_dom": f"{btc_dom:.1f}",
        "eth_dom": f"{eth_dom:.1f}",
        "other_dom": f"{other_dom:.1f}",
        "tvl": _fmt_usd(d["total_tvl_usd"]),
        "chains": str(d["chains"]),
        "venue_bars": venue_bars,
        "top_rows": coin_rows(co["top"]),
        "gainer_rows": coin_rows(co["top_gainers"]),
        "loser_rows": coin_rows(co["top_losers"]),
        "defi_rows": defi_rows,
    }
    out = _HTML_TEMPLATE
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


_TOKENS_DARK = (
    "--bg:#0b0f16;--panel:#121a26;--panel2:#1a2433;--line:#26313f;"
    "--txt:#e9eef5;--dim:#8493a8;--accent:#e0a740;--accent2:#f0c56a;"
    "--up:#2ebd85;--down:#e5484d;--btc:#f7931a;--eth:#8a92b2;"
)
_TOKENS_LIGHT = (
    "--bg:#eceff4;--panel:#ffffff;--panel2:#e6ebf2;--line:#d8e0ea;"
    "--txt:#141b26;--dim:#5b6a80;--accent:#a9720f;--accent2:#c68a1c;"
    "--up:#12a06a;--down:#d23a3f;--btc:#d97a08;--eth:#5a63a0;"
)

# Natural single-brace CSS: render_html substitutes {name} placeholders via
# str.replace, so no brace-escaping is needed.  {dark}/{light} are injected at
# import time below.
_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crocodile — Market Census</title>
<style>
:root { {dark} }
@media (prefers-color-scheme: light) { :root { {light} } }
:root[data-theme="dark"] { {dark} }
:root[data-theme="light"] { {light} }
:root {
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:15px;line-height:1.5;
  padding:32px 24px;max-width:1120px;margin:0 auto;-webkit-font-smoothing:antialiased}
.wm{display:flex;align-items:center;gap:11px;margin-bottom:2px}
.wm .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 22%,transparent)}
.wm h1{font-size:15px;font-weight:700;letter-spacing:.18em;text-transform:uppercase}
.wm h1 b{color:var(--accent);font-weight:700}
.lede{font-size:31px;font-weight:800;letter-spacing:-.02em;line-height:1.12;text-wrap:balance;
  max-width:20ch;margin:14px 0 4px}
.gen{color:var(--dim);font-size:12.5px;font-family:var(--mono);letter-spacing:.02em}
.readout{display:grid;grid-template-columns:repeat(5,1fr);border:1px solid var(--line);
  border-radius:12px;background:var(--panel);margin:24px 0;overflow:hidden;
  border-top:2px solid var(--accent)}
.cell{padding:16px 18px;border-left:1px solid var(--line)}
.cell:first-child{border-left:none}
.cell .k{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.09em}
.cell .v{font-family:var(--mono);font-size:27px;font-weight:600;letter-spacing:-.02em;
  margin-top:7px;font-variant-numeric:tabular-nums}
.cell .s{color:var(--dim);font-size:11.5px;margin-top:3px;font-family:var(--mono)}
.cell.hl .v{color:var(--accent)}
@media (max-width:780px){.readout{grid-template-columns:repeat(2,1fr)}
  .cell:nth-child(2){border-left:none}}
@media (max-width:440px){.readout{grid-template-columns:1fr}.cell{border-left:none;
  border-top:1px solid var(--line)}.cell:first-child{border-top:none}}
.grid{display:grid;grid-template-columns:1.35fr 1fr;gap:16px;margin-bottom:16px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:780px){.grid,.two{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:18px 20px;overflow-x:auto}
h2{font-size:11.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);
  font-weight:700;margin-bottom:14px}
.bar-row{display:flex;align-items:center;gap:12px;margin:8px 0;font-size:13px}
.bar-name{width:88px;flex:none;letter-spacing:.02em}
.bar-track{flex:1;height:7px;background:var(--panel2);border-radius:4px;overflow:hidden}
.bar-fill{display:block;height:100%;border-radius:4px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  transform-origin:left;animation:grow .9s cubic-bezier(.2,.7,.2,1) both}
.bar-val{width:72px;text-align:right;flex:none;color:var(--dim);
  font-family:var(--mono);font-variant-numeric:tabular-nums}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@media (prefers-reduced-motion:reduce){.bar-fill{animation:none}}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:none}
td.rank{color:var(--dim);width:34px;font-family:var(--mono);font-size:12px}
td.sym{font-weight:700;letter-spacing:.01em}
td.nm{color:var(--dim);max-width:150px;overflow:hidden;text-overflow:ellipsis}
td.px{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
td.ch{text-align:right;width:74px}
.chg{font-weight:600;font-family:var(--mono);font-variant-numeric:tabular-nums}
.chg.up{color:var(--up)} .chg.down{color:var(--down)}
.dom{height:10px;border-radius:5px;overflow:hidden;display:flex;margin:4px 0 12px}
.dom span{display:block;height:100%}
.dom .b{background:var(--btc)} .dom .e{background:var(--eth)} .dom .o{background:var(--panel2)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--dim);flex-wrap:wrap;font-family:var(--mono)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;
  vertical-align:middle}
.kinds{display:flex;gap:20px;flex-wrap:wrap;margin-top:14px;padding-top:14px;
  border-top:1px solid var(--line);font-size:12px;color:var(--dim)}
.kinds b{color:var(--txt);font-family:var(--mono);font-variant-numeric:tabular-nums;
  font-weight:600;margin-left:4px}
.note{color:var(--dim);font-size:13px;line-height:1.75}
.note b{color:var(--txt);font-weight:600}
code{font-family:var(--mono);font-size:.9em;color:var(--accent)}
.foot{color:var(--dim);font-size:12px;margin-top:22px;text-align:center;font-family:var(--mono)}
</style></head>
<body>
<div class="wm"><span class="dot"></span><h1>Crocodile <b>/</b> Market Census</h1></div>
<div class="lede">The entire crypto market, on one instrument.</div>
<div class="gen">{generated} · live · keyless public feeds</div>
<div class="readout">
  <div class="cell hl"><div class="k">Reachable venues</div><div class="v">{reachable}</div><div class="s">{native} native · {ccxt} ccxt</div></div>
  <div class="cell"><div class="k">Markets · top {majors}</div><div class="v">{total_markets}</div><div class="s">spot·perp·fut·opt</div></div>
  <div class="cell"><div class="k">Active coins</div><div class="v">{active_coins}</div><div class="s">whole universe</div></div>
  <div class="cell"><div class="k">Total market cap</div><div class="v">{mcap}</div><div class="s">24h vol {vol}</div></div>
  <div class="cell"><div class="k">DeFi TVL</div><div class="v">{tvl}</div><div class="s">{chains} chains</div></div>
</div>
<div class="grid">
  <div class="card"><h2>Markets per venue</h2>{venue_bars}
    <div class="kinds"><span>spot<b>{spot}</b></span><span>perpetual<b>{perp}</b></span><span>future<b>{future}</b></span><span>option<b>{option}</b></span></div>
  </div>
  <div class="card"><h2>Market dominance</h2>
    <div class="dom"><span class="b" style="width:{btc_dom}%"></span><span class="e" style="width:{eth_dom}%"></span><span class="o" style="width:{other_dom}%"></span></div>
    <div class="legend"><span><i style="background:var(--btc)"></i>BTC {btc_dom}%</span><span><i style="background:var(--eth)"></i>ETH {eth_dom}%</span><span><i style="background:var(--panel2)"></i>Other {other_dom}%</span></div>
    <h2 style="margin-top:20px">Top by market cap</h2>
    <table>{top_rows}</table>
  </div>
</div>
<div class="two">
  <div class="card"><h2>Top 24h gainers</h2><table>{gainer_rows}</table></div>
  <div class="card"><h2>Top 24h losers</h2><table>{loser_rows}</table></div>
</div>
<div class="grid" style="margin-top:16px">
  <div class="card"><h2>DeFi TVL by chain</h2>{defi_rows}</div>
  <div class="card"><h2>What this measures</h2>
    <p class="note">Every figure is live from keyless public APIs. <b>Venues</b> and their market counts come from ccxt <code>load_markets</code>; the <b>coin universe</b>, market cap and dominance from CoinGecko; <b>TVL</b> from DeFiLlama. Crocodile ingests any of it into one Parquet lake under a single record schema — a ccxt venue and a native connector are indistinguishable downstream.</p>
  </div>
</div>
<div class="foot">crocodile census — the whole market, quantified</div>
</body></html>""".replace("{dark}", _TOKENS_DARK).replace("{light}", _TOKENS_LIGHT)
