import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# The bridge is normally run as a standalone helper, not through run_fastapi.ps1.
# Default to the local Redis/Memurai cache so fetched iFinD quotes are not lost.
os.environ.setdefault("USE_REDIS", "1")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_DB", "0")

from backend.cache_store import redis_status
from backend.ifind_stream import get_ifind_push_quote, get_ifind_push_ticks, set_ifind_push_quote
from backend.main import finite, ifind_realtime_quote, normalize_a_share_symbol


def build_push_payload(symbol_info: dict[str, str], quote_data: dict[str, Any]) -> dict[str, Any]:
    quote = quote_data.get("quote") or {}
    return {
        "symbol": symbol_info["display"],
        "name": quote_data.get("name") or symbol_info["display"],
        "provider": "iFinD Push Bridge",
        "quoteTime": quote_data.get("quoteTime"),
        "price": finite(quote.get("price")),
        "previousClose": finite(quote.get("previousClose")),
        "open": finite(quote.get("open")),
        "dayHigh": finite(quote.get("dayHigh")),
        "dayLow": finite(quote.get("dayLow")),
        "volume": finite(quote.get("volume")),
        "amount": finite(quote.get("amount")),
        "change": finite(quote.get("change")),
        "changePercent": finite(quote.get("changePercent")),
        "volumeRatio": finite(quote.get("volumeRatio")),
        "committee": finite(quote.get("committee")),
        "commissionDiff": finite(quote.get("commissionDiff")),
        "tradeStatus": quote.get("tradeStatus"),
    }


async def push_once(symbol: str, ttl: int) -> dict[str, Any]:
    symbol_info = normalize_a_share_symbol(symbol)
    if not symbol_info:
        raise ValueError(f"Only A-share symbols are supported: {symbol}")
    quote_data = await ifind_realtime_quote(symbol_info, ttl=0)
    payload = build_push_payload(symbol_info, quote_data)
    if payload["price"] is None:
        raise RuntimeError(f"iFinD quote has no price for {symbol_info['display']}")
    saved = set_ifind_push_quote(symbol_info["display"], payload, ttl=ttl)
    if not get_ifind_push_quote(symbol_info["display"], max_age_seconds=max(ttl, 1)):
        raise RuntimeError(f"Redis push quote write failed: {redis_status()}")
    if not get_ifind_push_ticks(symbol_info["display"], limit=1):
        raise RuntimeError(f"Redis push tick write failed: {redis_status()}")
    return saved


async def run_bridge(symbols: list[str], interval: float, ttl: int, once: bool, concurrency: int) -> None:
    normalized = []
    for symbol in symbols:
        symbol_info = normalize_a_share_symbol(symbol)
        if not symbol_info:
            raise ValueError(f"Only A-share symbols are supported: {symbol}")
        normalized.append(symbol_info["display"])

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def push_with_limit(symbol: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
        # Multiple symbols should not wait for each other serially; bound the
        # concurrency so iFinD is not flooded when the watchlist grows.
        async with semaphore:
            try:
                return symbol, await push_once(symbol, ttl), None
            except Exception as exc:
                return symbol, None, exc

    while True:
        results = await asyncio.gather(*(push_with_limit(symbol) for symbol in normalized))
        for symbol, payload, error in results:
            if payload:
                print(
                    f"{payload['pushedAt']} {payload['symbol']} price={payload['price']} "
                    f"quoteTime={payload.get('quoteTime')} provider={payload.get('provider')}"
                )
            elif error:
                print(f"{symbol} push failed: {error}", file=sys.stderr)
        if once:
            return
        await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write iFinD real-time A-share quotes into the Redis push cache.")
    parser.add_argument("symbols", nargs="+", help="A-share symbols, e.g. 600519 SH688981 300033.SZ")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds.")
    parser.add_argument("--ttl", type=int, default=6, help="Redis quote TTL in seconds.")
    parser.add_argument("--concurrency", type=int, default=5, help="Maximum concurrent iFinD quote requests.")
    parser.add_argument("--once", action="store_true", help="Run once and exit.")
    args = parser.parse_args()
    asyncio.run(run_bridge(args.symbols, args.interval, args.ttl, args.once, args.concurrency))


if __name__ == "__main__":
    main()
