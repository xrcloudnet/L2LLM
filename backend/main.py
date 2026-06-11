import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import akshare as ak
import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.cache_store import redis_get, redis_set, redis_status
from backend.analysis import run_local_analysis
from backend.analysis.macd import build_seconds_macd_signal
from backend.db import init_db, list_ai_analysis, list_market_candles, save_ai_analysis, save_market_candles, storage_status
from backend.ifind_stream import (
    get_ifind_push_quote,
    get_ifind_push_ticks,
    ifind_push_enabled,
    parse_time,
    pushed_quote_to_ifind_shape,
)
from backend.providers.common import (
    CHINA_TZ,
    US_TZ,
    china_market_day_ms,
    china_market_time_ms,
    df_records,
    finite,
    is_aggregated_interval,
    normalize_a_share_symbol,
    normalize_interval,
    normalize_market_response_symbol,
    numeric_series,
    range_window_ms,
    resample_candles,
    synthesize_order_book,
)
from backend.providers.local_cache import fallback_to_local_market, local_cached_market_payload


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "public"
PORTFOLIO_FILE = ROOT / "data" / "portfolio.json"
PORT = int(os.getenv("PORT", "5177"))
PORTFOLIO_MARKETS = {
    "cn": {"label": "中国", "currency": "CNY"},
    "us": {"label": "美国", "currency": "USD"},
    "hk": {"label": "香港", "currency": "HKD"},
}

app = FastAPI(title="L2LLM Stock AI", version="0.2.0")
cache: dict[str, tuple[float, Any]] = {}
third_party_ai_pending: set[str] = set()
request_logger = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def log_request_timing(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    started = time.perf_counter()
    request_logger.info("REQ start %s %s", request.method, request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    try:
        response = await call_next(request)
    except Exception:
        elapsed = time.perf_counter() - started
        request_logger.exception("REQ error %s %s %.3fs", request.method, request.url.path, elapsed)
        raise
    elapsed = time.perf_counter() - started
    response.headers["X-Process-Time"] = f"{elapsed:.3f}"
    request_logger.info("REQ end %s %s status=%s %.3fs", request.method, request.url.path, response.status_code, elapsed)
    return response


@app.on_event("startup")
async def startup() -> None:
    await asyncio.to_thread(init_db)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cache_get(key: str, ttl: float) -> Any | None:
    cached = redis_get(key)
    if cached is not None:
        return cached
    hit = cache.get(key)
    if not hit:
        return None
    saved_at = hit[0]
    value = hit[1]
    stored_ttl = hit[2] if len(hit) > 2 else ttl
    if time.time() - saved_at > stored_ttl:
        return None
    return value


def cache_set(key: str, value: Any, ttl: float = 30) -> Any:
    cache[key] = (time.time(), value, ttl)
    redis_set(key, value, ttl)
    return value


def schedule_market_candle_save(
    *,
    market: str,
    symbol: str,
    interval: str,
    provider: str,
    candles: list[dict[str, Any]],
) -> None:
    if not candles:
        return
    asyncio.create_task(
        asyncio.to_thread(
            save_market_candles,
            market=market,
            symbol=symbol,
            interval=interval,
            provider=provider,
            candles=candles,
        )
    )



def load_portfolio() -> dict[str, Any]:
    if not PORTFOLIO_FILE.exists():
        return {"cash": 0, "trades": []}
    return json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8-sig"))


def save_portfolio(data: dict[str, Any]) -> None:
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def portfolio_symbol_key(symbol: str) -> str:
    a_share = normalize_a_share_symbol(symbol)
    return a_share["display"] if a_share else symbol.strip().upper()


def normalize_portfolio_market(market: Any, symbol: str = "") -> str:
    value = str(market or "").strip().lower()
    if value in PORTFOLIO_MARKETS:
        return value
    raw = symbol.strip().upper()
    if normalize_a_share_symbol(raw):
        return "cn"
    if raw.endswith(".HK") or raw.startswith("HK"):
        return "hk"
    return "us"


def normalize_portfolio_symbol(symbol: str, market: str) -> str:
    raw = symbol.strip().upper()
    if market == "cn":
        return portfolio_symbol_key(raw)
    if market == "hk":
        if raw.endswith(".HK"):
            return raw
        if raw.startswith("HK") and raw[2:].isdigit():
            return f"{int(raw[2:]):04d}.HK"
        if raw.isdigit():
            return f"{int(raw):04d}.HK"
    return raw


async def fetch_portfolio_quote(symbol: str, market: str = "") -> dict[str, Any]:
    market = normalize_portfolio_market(market, symbol)
    key = normalize_portfolio_symbol(symbol, market)
    cached = cache_get(f"portfolio:quote:{market}:{key}", 8)
    if cached is not None:
        return cached

    a_share = normalize_a_share_symbol(key) if market == "cn" else None
    if a_share:
        market_data = await fetch_china_market(a_share, "1d", "1m")
    else:
        try:
            market_data = await fetch_moomoo_market(key, "5d", "1d")
        except Exception as moomoo_exc:
            try:
                market_data = await fetch_yahoo_market(key, "5d", "1d")
            except Exception as yahoo_exc:
                try:
                    market_data = await fetch_twelve_data_market(key, "5d", "1d")
                except Exception as twelve_exc:
                    raise RuntimeError(
                        f"Moomoo failed: {moomoo_exc}; Yahoo failed: {yahoo_exc}; Twelve Data failed: {twelve_exc}"
                    ) from twelve_exc
    quote = {
        "symbol": market_data["symbol"],
        "name": market_data.get("name") or market_data["symbol"],
        "currency": market_data.get("currency", ""),
        "market": market,
        "price": finite(market_data.get("quote", {}).get("price")),
        "updatedAt": market_data.get("updatedAt"),
    }
    return cache_set(f"portfolio:quote:{market}:{key}", quote, 8)


def empty_portfolio_summary() -> dict[str, Any]:
    return {
        "totalCost": 0.0,
        "marketValue": 0.0,
        "unrealizedPnl": 0.0,
        "unrealizedReturn": 0,
        "realizedPnl": 0.0,
        "totalPnl": 0.0,
        "totalReturn": 0,
        "positionCount": 0,
        "tradeCount": 0,
    }


def summarize_portfolio_rows(positions: list[dict[str, Any]], trades: list[dict[str, Any]], realized_pnl: float = 0.0) -> dict[str, Any]:
    total_cost = sum(finite(row.get("cost")) or 0 for row in positions)
    total_value = sum(finite(row.get("marketValue")) or 0 for row in positions)
    unrealized_pnl = total_value - total_cost
    return {
        "totalCost": total_cost,
        "marketValue": total_value,
        "unrealizedPnl": unrealized_pnl,
        "unrealizedReturn": unrealized_pnl / total_cost * 100 if total_cost else 0,
        "realizedPnl": realized_pnl,
        "totalPnl": unrealized_pnl + realized_pnl,
        "totalReturn": (unrealized_pnl + realized_pnl) / total_cost * 100 if total_cost else 0,
        "positionCount": len(positions),
        "tradeCount": len(trades),
    }


async def build_portfolio_snapshot(market_filter: str = "all") -> dict[str, Any]:
    market_filter = str(market_filter or "all").lower()
    if market_filter not in {"all", *PORTFOLIO_MARKETS.keys()}:
        raise HTTPException(status_code=400, detail="market must be all, cn, us, or hk")

    data = load_portfolio()
    trades = data.get("trades", [])
    positions: dict[tuple[str, str], dict[str, Any]] = {}
    realized_by_market = {market: 0.0 for market in PORTFOLIO_MARKETS}

    for trade in trades:
        market = normalize_portfolio_market(trade.get("market"), str(trade.get("symbol", "")))
        symbol = normalize_portfolio_symbol(str(trade.get("symbol", "")), market)
        side = str(trade.get("side", "buy")).lower()
        quantity = finite(trade.get("quantity")) or 0
        price = finite(trade.get("price")) or 0
        if quantity <= 0 or price <= 0:
            continue
        key = (market, symbol)
        position = positions.setdefault(key, {"market": market, "symbol": symbol, "quantity": 0.0, "cost": 0.0, "realizedPnl": 0.0, "trades": []})
        signed_qty = quantity if side == "buy" else -quantity
        if side == "sell" and position["quantity"] > 0:
            avg_cost = position["cost"] / position["quantity"]
            closed_qty = min(quantity, position["quantity"])
            pnl = (price - avg_cost) * closed_qty
            position["realizedPnl"] += pnl
            realized_by_market[market] += pnl
            position["cost"] -= avg_cost * closed_qty
        elif side == "buy":
            position["cost"] += quantity * price
        position["quantity"] += signed_qty
        position["trades"].append({**trade, "market": market, "symbol": symbol})

    open_positions = {key: pos for key, pos in positions.items() if pos["quantity"] > 0}
    quotes = {}
    for (market, symbol) in open_positions:
        try:
            quotes[(market, symbol)] = await fetch_portfolio_quote(symbol, market)
        except Exception as exc:
            quotes[(market, symbol)] = {"symbol": symbol, "name": symbol, "currency": PORTFOLIO_MARKETS[market]["currency"], "price": None, "market": market, "error": str(exc)}

    position_rows = []
    for (market, symbol), pos in open_positions.items():
        quote = quotes.get((market, symbol), {})
        current_price = finite(quote.get("price"))
        avg_cost = pos["cost"] / pos["quantity"] if pos["quantity"] else 0
        market_value = (current_price or 0) * pos["quantity"]
        pnl = market_value - pos["cost"] if current_price is not None else None
        pnl_percent = pnl / pos["cost"] * 100 if pnl is not None and pos["cost"] else None
        position_rows.append(
            {
                "market": market,
                "marketLabel": PORTFOLIO_MARKETS[market]["label"],
                "symbol": symbol,
                "name": quote.get("name") or symbol,
                "currency": quote.get("currency") or PORTFOLIO_MARKETS[market]["currency"],
                "quantity": pos["quantity"],
                "avgCost": avg_cost,
                "currentPrice": current_price,
                "marketValue": market_value,
                "cost": pos["cost"],
                "unrealizedPnl": pnl,
                "unrealizedReturn": pnl_percent,
                "realizedPnl": pos["realizedPnl"],
                "quoteError": quote.get("error"),
            }
        )

    for row in position_rows:
        market_value = sum((finite(item.get("marketValue")) or 0) for item in position_rows if item["market"] == row["market"])
        row["weight"] = row["marketValue"] / market_value * 100 if market_value else 0

    trade_rows = []
    for index, trade in enumerate(trades, start=1):
        market = normalize_portfolio_market(trade.get("market"), str(trade.get("symbol", "")))
        symbol = normalize_portfolio_symbol(str(trade.get("symbol", "")), market)
        quote = quotes.get((market, symbol))
        if quote is None:
            try:
                quote = await fetch_portfolio_quote(symbol, market)
            except Exception:
                quote = {"symbol": symbol, "price": None}
        current_price = finite(quote.get("price"))
        entry_price = finite(trade.get("price")) or 0
        side = str(trade.get("side", "buy")).lower()
        take_profit = finite(trade.get("takeProfit"))
        stop_loss = finite(trade.get("stopLoss"))
        trade_return = None
        if current_price is not None and entry_price:
            direction = 1 if side == "buy" else -1
            trade_return = (current_price - entry_price) / entry_price * 100 * direction
        trade_rows.append(
            {
                "id": trade.get("id") or index,
                "market": market,
                "marketLabel": PORTFOLIO_MARKETS[market]["label"],
                "symbol": symbol,
                "side": side,
                "quantity": finite(trade.get("quantity")) or 0,
                "price": entry_price,
                "time": trade.get("time"),
                "takeProfit": take_profit,
                "stopLoss": stop_loss,
                "currentPrice": current_price,
                "returnPercent": trade_return,
                "distanceToTakeProfit": (take_profit - current_price) / current_price * 100 if take_profit and current_price else None,
                "distanceToStopLoss": (current_price - stop_loss) / current_price * 100 if stop_loss and current_price else None,
            }
        )

    market_summaries = {}
    for market, meta in PORTFOLIO_MARKETS.items():
        market_positions = [row for row in position_rows if row["market"] == market]
        market_trades = [row for row in trade_rows if row["market"] == market]
        market_summaries[market] = {
            "label": meta["label"],
            "currency": meta["currency"],
            **summarize_portfolio_rows(market_positions, market_trades, realized_by_market.get(market, 0.0)),
        }

    selected_positions = position_rows if market_filter == "all" else [row for row in position_rows if row["market"] == market_filter]
    selected_trades = trade_rows if market_filter == "all" else [row for row in trade_rows if row["market"] == market_filter]
    selected_realized = sum(realized_by_market.values()) if market_filter == "all" else realized_by_market.get(market_filter, 0.0)
    summary = summarize_portfolio_rows(selected_positions, selected_trades, selected_realized)
    return {
        "updatedAt": now_iso(),
        "market": market_filter,
        "cash": finite(data.get("cash")) or 0,
        "summary": summary,
        "markets": market_summaries,
        "positions": sorted(selected_positions, key=lambda row: row["marketValue"], reverse=True),
        "trades": selected_trades,
    }



async def http_json(url: str, *, ttl: float, key: str, headers: dict[str, str] | None = None) -> Any:
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Timeout connecting to data source: {url}") from exc
    except httpx.TransportError as exc:
        raise RuntimeError(f"Network error connecting to data source: {url}: {exc}") from exc
    try:
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ").strip()
            raise RuntimeError(f"HTTP {response.status_code} from data source: {url}. {body}")
        return cache_set(key, response.json(), ttl)
    finally:
        await response.aclose()


async def http_text(url: str, *, ttl: float, key: str, headers: dict[str, str] | None = None, encoding: str = "utf-8") -> str:
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
            response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Timeout connecting to data source: {url}") from exc
    except httpx.TransportError as exc:
        raise RuntimeError(f"Network error connecting to data source: {url}: {exc}") from exc
    try:
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ").strip()
            raise RuntimeError(f"HTTP {response.status_code} from data source: {url}. {body}")
        text = response.content.decode(encoding, errors="replace")
        return cache_set(key, text, ttl)
    finally:
        await response.aclose()


async def http_json_params(url: str, *, params: dict[str, Any], ttl: float, key: str) -> Any:
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Timeout connecting to data source: {url}") from exc
    except httpx.TransportError as exc:
        raise RuntimeError(f"Network error connecting to data source: {url}: {exc}") from exc
    try:
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ").strip()
            raise RuntimeError(f"HTTP {response.status_code} from data source: {response.url}. {body}")
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error: {data.get('message') or data.get('code') or data}")
        return cache_set(key, data, ttl)
    finally:
        await response.aclose()


def ifind_enabled() -> bool:
    return os.getenv("USE_IFIND") == "1"


def ifind_realtime_ttl() -> float:
    return finite(os.getenv("IFIND_REALTIME_TTL")) or 3


def ifind_a_share_code(symbol_info: dict[str, str]) -> str:
    return f"{symbol_info['code']}.{symbol_info['exchange'].upper()}"


async def ifind_access_token() -> str:
    token = os.getenv("IFIND_ACCESS_TOKEN")
    if token:
        return token

    cached = cache_get("ifind:access_token", 60 * 60)
    if cached:
        return cached

    refresh_token = os.getenv("IFIND_REFRESH_TOKEN")
    if not refresh_token:
        raise RuntimeError("IFIND_ACCESS_TOKEN or IFIND_REFRESH_TOKEN is required.")

    token_url = "https://quantapi.51ifind.com/api/v1/get_access_token"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                token_url,
                headers={"Content-Type": "application/json", "refresh_token": refresh_token},
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError("Timeout connecting to iFinD token API") from exc
    except httpx.TransportError as exc:
        raise RuntimeError(f"Network error connecting to iFinD token API: {exc}") from exc
    if response.status_code >= 400:
        body = response.text[:300].replace("\n", " ").strip()
        raise RuntimeError(f"iFinD token HTTP {response.status_code}: {body}")
    data = response.json()
    if data.get("errorcode") not in (None, 0):
        raise RuntimeError(f"iFinD token error {data.get('errorcode')}: {data.get('errmsg') or data}")
    access_token = ((data.get("data") or {}).get("access_token")) or data.get("access_token")
    if not access_token:
        raise RuntimeError("iFinD token response does not contain access_token.")
    return cache_set("ifind:access_token", access_token, 60 * 60)


def ifind_token_error(data: dict[str, Any]) -> bool:
    return data.get("errorcode") in {-1010, -1302}


async def ifind_post(path: str, payload: dict[str, Any], *, ttl: float, key: str, retry: bool = True) -> dict[str, Any]:
    if not ifind_enabled():
        raise RuntimeError("USE_IFIND is not 1.")
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached

    token = await ifind_access_token()
    url = f"https://quantapi.51ifind.com/api/v1/{path}"
    try:
        async with httpx.AsyncClient(timeout=16.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "access_token": token, "Accept-Encoding": "gzip,deflate"},
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"Timeout connecting to iFinD API: {path}") from exc
    except httpx.TransportError as exc:
        raise RuntimeError(f"Network error connecting to iFinD API {path}: {exc}") from exc
    if response.status_code >= 400:
        body = response.text[:300].replace("\n", " ").strip()
        raise RuntimeError(f"iFinD HTTP {response.status_code}: {body}")
    data = response.json()
    if isinstance(data, dict) and ifind_token_error(data) and retry and os.getenv("IFIND_REFRESH_TOKEN"):
        cache.pop("ifind:access_token", None)
        return await ifind_post(path, payload, ttl=ttl, key=key, retry=False)
    if data.get("errorcode") not in (None, 0):
        raise RuntimeError(f"iFinD error {data.get('errorcode')}: {data.get('errmsg') or data}")
    return cache_set(key, data, ttl)


def ifind_table_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    tables = data.get("tables") or data.get("data") or []
    if isinstance(tables, dict):
        tables = [tables]

    rows: list[dict[str, Any]] = []
    for table_item in tables:
        table = table_item.get("table") if isinstance(table_item, dict) else table_item
        thscode = table_item.get("thscode") if isinstance(table_item, dict) else None
        table_time = table_item.get("time") if isinstance(table_item, dict) else None
        if isinstance(table, list):
            for index, row in enumerate(table):
                if isinstance(row, dict):
                    time_value = table_time[index] if isinstance(table_time, list) and index < len(table_time) else table_time
                    rows.append({"thscode": thscode, "time": time_value, **row})
        elif isinstance(table, dict):
            values = dict(table)
            if table_time is not None and "time" not in values:
                values["time"] = table_time
            lengths = [len(value) for value in values.values() if isinstance(value, list)]
            if lengths:
                for index in range(max(lengths)):
                    row = {"thscode": thscode}
                    for column, value in values.items():
                        row[column] = value[index] if isinstance(value, list) and index < len(value) else value
                    rows.append(row)
            else:
                rows.append({"thscode": thscode, **values})
    return rows


def ifind_first_row(data: dict[str, Any]) -> dict[str, Any]:
    rows = ifind_table_rows(data)
    if not rows:
        raise RuntimeError("iFinD returned empty table.")
    return rows[-1]


def row_get(row: dict[str, Any], *names: str) -> Any:
    lower_map = {str(key).lower().replace(" ", ""): value for key, value in row.items()}
    for name in names:
        if name in row:
            return row[name]
        key = name.lower().replace(" ", "")
        if key in lower_map:
            return lower_map[key]
    return None


def ifind_range_dates(range_name: str) -> tuple[str, str]:
    end = datetime.now(CHINA_TZ)
    if range_name == "ytd":
        start = datetime(end.year, 1, 1, tzinfo=CHINA_TZ)
    elif range_name == "all":
        start = end - timedelta(days=365 * 20)
    else:
        start = end - timedelta(milliseconds=range_window_ms(range_name))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def ifind_high_frequency_times(range_name: str) -> tuple[str, str]:
    end = datetime.now(CHINA_TZ)
    days = min(max(range_window_ms(range_name) // (24 * 60 * 60 * 1000), 1), 30)
    start = end - timedelta(days=days)
    return start.strftime("%Y-%m-%d 09:15:00"), end.strftime("%Y-%m-%d %H:%M:%S")


def ifind_history_interval(interval: str) -> str:
    return {
        "1wk": "W",
        "1w": "W",
        "1week": "W",
        "1mo": "M",
        "3mo": "Q",
        "6mo": "S",
    }.get(interval, "D")


def ifind_minute_interval(interval: str) -> str:
    return {"1m": "1", "2m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}.get(interval, "1")


async def run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def run_blocking_timeout(timeout: float, func, *args, **kwargs):
    return await asyncio.wait_for(run_blocking(func, *args, **kwargs), timeout=timeout)



async def ak_bid_ask(symbol: str) -> dict[str, Any]:
    key = f"ak:bidask:{symbol}"
    cached = cache_get(key, 3)
    if cached is not None:
        return cached

    df = await run_blocking_timeout(6, ak.stock_bid_ask_em, symbol=symbol)
    data = {str(row["item"]): finite(row["value"]) for row in df_records(df)}
    bids = [
        {"price": data.get(f"buy_{level}"), "size": data.get(f"buy_{level}_vol")}
        for level in range(1, 6)
    ]
    asks = [
        {"price": data.get(f"sell_{level}"), "size": data.get(f"sell_{level}_vol")}
        for level in range(1, 6)
    ]
    bids = [row for row in bids if row["price"] and row["size"] is not None]
    asks = [row for row in asks if row["price"] and row["size"] is not None]
    bid_total = sum(row["size"] for row in bids)
    ask_total = sum(row["size"] for row in asks)

    result = {
        "quote": {
            "price": data.get("最新"),
            "previousClose": data.get("昨收"),
            "open": data.get("今开"),
            "dayHigh": data.get("最高"),
            "dayLow": data.get("最低"),
            "volume": (data.get("总手") or 0) * 100,
            "amount": data.get("金额"),
            "change": data.get("涨跌"),
            "changePercent": data.get("涨幅"),
        },
        "orderBook": {
            "provider": "AKShare stock_bid_ask_em",
            "note": "A股盘口来自 AKShare 东方财富五档买卖盘；免费源可能存在延迟或快照缺口。",
            "bids": bids,
            "asks": asks,
            "imbalance": (bid_total - ask_total) / max(bid_total + ask_total, 1),
        },
    }
    return cache_set(key, result, 3)


async def a_share_fund_flow(symbol_info: dict[str, str]) -> dict[str, Any]:
    key = f"ak:fundflow:{symbol_info['display']}"
    cached = cache_get(key, 60)
    if cached is not None:
        return cached

    df = await run_blocking_timeout(
        8,
        ak.stock_individual_fund_flow,
        stock=symbol_info["code"],
        market=symbol_info["exchange"],
    )
    records = df_records(df)
    if not records:
        raise RuntimeError("AKShare returned empty fund flow")

    latest = records[-1]
    recent = records[-5:]
    ratio_values = [finite(row.get("主力净流入-净占比")) for row in recent]
    ratio_values = [value for value in ratio_values if value is not None]
    latest_ratio = finite(latest.get("主力净流入-净占比")) or 0
    avg_ratio = sum(ratio_values) / len(ratio_values) if ratio_values else latest_ratio
    super_ratio = finite(latest.get("超大单净流入-净占比")) or 0
    large_ratio = finite(latest.get("大单净流入-净占比")) or 0
    small_ratio = finite(latest.get("小单净流入-净占比")) or 0

    result = {
        "source": "AKShare Eastmoney fund flow",
        "quality": "official-like",
        "estimated": False,
        "date": str(latest.get("日期") or ""),
        "close": finite(latest.get("收盘价")),
        "changePercent": finite(latest.get("涨跌幅")),
        "mainNetInflow": finite(latest.get("主力净流入-净额")) or 0,
        "mainNetInflowRatio": latest_ratio,
        "superLargeNetInflow": finite(latest.get("超大单净流入-净额")) or 0,
        "superLargeNetInflowRatio": super_ratio,
        "largeNetInflow": finite(latest.get("大单净流入-净额")) or 0,
        "largeNetInflowRatio": large_ratio,
        "mediumNetInflow": finite(latest.get("中单净流入-净额")) or 0,
        "mediumNetInflowRatio": finite(latest.get("中单净流入-净占比")) or 0,
        "smallNetInflow": finite(latest.get("小单净流入-净额")) or 0,
        "smallNetInflowRatio": small_ratio,
        "ddx": latest_ratio,
        "ddy": latest_ratio - avg_ratio,
        "ddz": super_ratio + large_ratio - abs(small_ratio) * 0.25,
        "recentMainNetInflow": sum(finite(row.get("主力净流入-净额")) or 0 for row in recent),
        "recentMainNetInflowRatioAvg": avg_ratio,
    }
    return cache_set(key, result, 60)


async def ifind_realtime_quote(symbol_info: dict[str, str], ttl: float = 3) -> dict[str, Any]:
    code = ifind_a_share_code(symbol_info)
    indicators = ",".join(
        [
            "tradeDate",
            "tradeTime",
            "preClose",
            "open",
            "high",
            "low",
            "latest",
            "change",
            "changeRatio",
            "volume",
            "amount",
            "vol_ratio",
            "committee",
            "commission_diff",
            "tradeStatus",
        ]
    )
    data = await ifind_post(
        "real_time_quotation",
        {"codes": code, "indicators": indicators},
        ttl=ttl,
        key=f"ifind:rt:{code}",
    )
    row = ifind_first_row(data)
    trade_date = row_get(row, "tradeDate", "date", "日期")
    trade_time = row_get(row, "tradeTime", "time", "时间")
    quote_time = f"{trade_date or ''} {trade_time or ''}".strip() or None
    latest = finite(row_get(row, "latest", "最新价"))
    previous = finite(row_get(row, "preClose", "前收盘价"))
    change = finite(row_get(row, "change", "涨跌"))
    if change is None and latest is not None and previous is not None:
        change = latest - previous
    change_percent = finite(row_get(row, "changeRatio", "涨跌幅"))
    if change_percent is None and change is not None and previous:
        change_percent = change / previous * 100

    return {
        "name": symbol_info["display"],
        "quoteTime": quote_time,
        "quote": {
            "price": latest,
            "previousClose": previous,
            "open": finite(row_get(row, "open", "开盘价")),
            "dayHigh": finite(row_get(row, "high", "最高价")),
            "dayLow": finite(row_get(row, "low", "最低价")),
            "volume": finite(row_get(row, "volume", "成交量")),
            "amount": finite(row_get(row, "amount", "成交额")),
            "change": change,
            "changePercent": change_percent,
            "volumeRatio": finite(row_get(row, "vol_ratio", "量比")),
            "committee": finite(row_get(row, "committee", "委比")),
            "commissionDiff": finite(row_get(row, "commission_diff", "委差")),
            "tradeStatus": row_get(row, "tradeStatus", "交易状态"),
        },
    }


def normalize_ifind_candles(rows: list[dict[str, Any]], range_name: str) -> list[dict[str, Any]]:
    candles = []
    cutoff = int(time.time() * 1000) - range_window_ms(range_name)
    for row in rows:
        timestamp_value = row_get(row, "time", "datetime", "date", "tradeDate", "日期", "时间")
        if timestamp_value is None:
            date_value = row_get(row, "tradeDate")
            time_value = row_get(row, "tradeTime")
            timestamp_value = f"{date_value or ''} {time_value or ''}".strip()
        timestamp = china_market_time_ms(timestamp_value)
        if timestamp is None:
            continue
        if range_name != "1d" and timestamp < cutoff:
            continue
        candle = {
            "time": timestamp,
            "open": finite(row_get(row, "open", "开盘价")),
            "high": finite(row_get(row, "high", "最高价")),
            "low": finite(row_get(row, "low", "最低价")),
            "close": finite(row_get(row, "close", "收盘价", "latest", "最新价")),
            "volume": finite(row_get(row, "volume", "成交量")),
            "amount": finite(row_get(row, "amount", "成交额")),
        }
        if all(candle[key] is not None for key in ["open", "high", "low", "close"]):
            candles.append(candle)
    return sorted(candles, key=lambda item: item["time"])


async def ifind_candles(symbol_info: dict[str, str], range_name: str, interval: str) -> list[dict[str, Any]]:
    code = ifind_a_share_code(symbol_info)
    if interval in {"1m", "2m", "5m", "15m", "30m", "60m"}:
        starttime, endtime = ifind_high_frequency_times(range_name)
        data = await ifind_post(
            "high_frequency",
            {
                "codes": code,
                "indicators": "open,high,low,close,volume,amount,changeRatio,buyVolume,sellVolume",
                "starttime": starttime,
                "endtime": endtime,
                "functionpara": {
                    "Interval": ifind_minute_interval(interval),
                    "Fill": "Original",
                    "Timeformat": "BeiJingTime",
                    "CPS": "no",
                },
            },
            ttl=6,
            key=f"ifind:hf:{code}:{range_name}:{interval}",
        )
    else:
        startdate, enddate = ifind_range_dates(range_name)
        data = await ifind_post(
            "cmd_history_quotation",
            {
                "codes": code,
                "indicators": "preClose,open,high,low,close,change,changeRatio,volume,amount,turnoverRatio",
                "startdate": startdate,
                "enddate": enddate,
                "functionpara": {
                    "Interval": ifind_history_interval(interval),
                    "CPS": "2",
                    "Currency": "RMB",
                    "Fill": "Blank",
                },
            },
            ttl=20,
            key=f"ifind:hist:{code}:{range_name}:{interval}",
        )
    candles = normalize_ifind_candles(ifind_table_rows(data), range_name)
    return candles[-900:] if range_name != "1d" else candles[-320:]


async def fetch_ifind_a_share_market(symbol_info: dict[str, str], range_name: str, interval: str) -> dict[str, Any]:
    if not ifind_enabled():
        raise RuntimeError("USE_IFIND is not 1.")

    phase_started = time.perf_counter()
    pushed_quote = get_ifind_push_quote(symbol_info["display"])
    if pushed_quote:
        quote_data = pushed_quote_to_ifind_shape(pushed_quote)
        candles = await ifind_candles(symbol_info, range_name, interval)
    else:
        quote_data, candles = await asyncio.gather(
            ifind_realtime_quote(symbol_info),
            ifind_candles(symbol_info, range_name, interval),
        )
    request_logger.info("MARKET phase ifind quote+candles %s %.3fs", symbol_info["display"], time.perf_counter() - phase_started)
    if not candles:
        raise RuntimeError("iFinD returned empty K-line data.")
    quote = quote_data["quote"]
    quote_time = quote_data.get("quoteTime")
    if candles:
        quote["dayHigh"] = quote.get("dayHigh") or max(c["high"] for c in candles if c["high"] is not None)
        quote["dayLow"] = quote.get("dayLow") or min(c["low"] for c in candles if c["low"] is not None)
        quote["open"] = quote.get("open") or candles[0]["open"]
        quote["price"] = quote.get("price") or candles[-1]["close"]

    if interval == "1d":
        candles = merge_realtime_daily_candle(candles, quote, quote_time)
    elif interval in {"1wk", "1w", "1week"}:
        candles = merge_realtime_weekly_candle(candles, quote, quote_time)

    phase_started = time.perf_counter()
    try:
        order_book = (await sina_quote(symbol_info))["orderBook"]
        order_book["note"] = f"{order_book.get('note', '')} iFinD行情优先，盘口使用新浪五档兜底。".strip()
    except Exception:
        order_book = synthesize_order_book(finite(quote.get("price")), candles)
        order_book["note"] = "iFinD行情优先；盘口源不可用，当前使用估算盘口。"

    request_logger.info("MARKET phase order_book %s %.3fs", symbol_info["display"], time.perf_counter() - phase_started)

    phase_started = time.perf_counter()
    try:
        fund_flow = await a_share_fund_flow(symbol_info)
    except Exception:
        fund_flow = None
    request_logger.info("MARKET phase fund_flow %s %.3fs", symbol_info["display"], time.perf_counter() - phase_started)

    return {
        "symbol": symbol_info["display"],
        "name": quote_data.get("name") or symbol_info["display"],
        "exchange": "Shanghai Stock Exchange" if symbol_info["exchange"] == "sh" else "Shenzhen Stock Exchange",
        "currency": "CNY",
        "marketType": "cn",
        "marketState": "A股",
        "provider": "iFinD Push + Pandas" if pushed_quote else "iFinD HTTP API + Pandas",
        "delayed": False,
        "updatedAt": now_iso(),
        "quote": quote,
        "candles": candles,
        "orderBook": order_book,
        "fundFlow": fund_flow,
        "quoteTime": quote_time,
    }


async def fetch_china_market(symbol_info: dict[str, str], range_name: str, interval: str) -> dict[str, Any]:
    try:
        return await fetch_ifind_a_share_market(symbol_info, range_name, interval)
    except Exception as ifind_exc:
        payload = await fetch_a_share_market(symbol_info, range_name, interval)
        if ifind_enabled():
            payload["provider"] = f"{payload['provider']} (iFinD fallback: {ifind_exc})"
            payload["orderBook"]["note"] = f"{payload['orderBook'].get('note', '')} iFinD failed, fallback to AKShare/Eastmoney/Sina.".strip()
        return payload



async def ak_minute_candles(symbol_info: dict[str, str], range_name: str, interval: str) -> list[dict[str, Any]]:
    period = normalize_interval(interval)
    key = f"ak:kline:{symbol_info['display']}:{range_name}:{period}"
    cached = cache_get(key, 6)
    if cached is not None:
        return cached

    if period in {"101", "102", "103"}:
        df = await run_blocking_timeout(8, ak.stock_zh_a_daily, symbol=symbol_info["sina"], adjust="qfq")
    else:
        try:
            df = await run_blocking_timeout(8, ak.stock_zh_a_minute, symbol=symbol_info["sina"], period=period, adjust="qfq")
        except Exception:
            df = await run_blocking_timeout(8, ak.stock_zh_a_hist_min_em, symbol=symbol_info["code"], period=period, adjust="qfq")

    candles = normalize_ak_candles(df, range_name)
    if period in {"102", "103"}:
        candles = resample_candles(candles, interval)
    return cache_set(key, candles, 6)


def normalize_ak_candles(df: pd.DataFrame, range_name: str) -> list[dict[str, Any]]:
    if df.empty:
        return []
    renamed = df.rename(
        columns={
            "时间": "time",
            "日期": "time",
            "day": "time",
            "date": "time",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
    )
    records = df_records(renamed)
    candles = []
    cutoff = int(time.time() * 1000) - range_window_ms(range_name)
    for row in records:
        timestamp = china_market_time_ms(row.get("time"))
        if timestamp is None:
            continue
        if range_name != "1d" and timestamp < cutoff:
            continue
        candle = {
            "time": timestamp,
            "open": finite(row.get("open")),
            "high": finite(row.get("high")),
            "low": finite(row.get("low")),
            "close": finite(row.get("close")),
            "volume": finite(row.get("volume")),
            "amount": finite(row.get("amount")),
        }
        if all(candle[key] is not None for key in ["open", "high", "low", "close"]):
            candles.append(candle)
    return candles[-900:] if range_name != "1d" else candles[-320:]


async def eastmoney_candles(symbol_info: dict[str, str], range_name: str, interval: str) -> list[dict[str, Any]]:
    klt = normalize_interval(interval)
    source_interval = interval
    if klt in {"102", "103"}:
        klt = "101"
        source_interval = "1d"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={symbol_info['secid']}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt={klt}&fqt=1&beg=0&end=20500101"
    )
    raw = await http_json(url, ttl=6, key=f"em:kline:{symbol_info['secid']}:{range_name}:{klt}")
    lines = raw.get("data", {}).get("klines", [])
    cutoff = int(time.time() * 1000) - range_window_ms(range_name)
    candles = []
    for line in lines:
        parts = line.split(",")
        timestamp = china_market_time_ms(parts[0])
        if timestamp is None:
            continue
        if range_name != "1d" and timestamp < cutoff:
            continue
        candles.append(
            {
                "time": timestamp,
                "open": finite(parts[1]),
                "close": finite(parts[2]),
                "high": finite(parts[3]),
                "low": finite(parts[4]),
                "volume": (finite(parts[5]) or 0) * 100,
                "amount": finite(parts[6]),
            }
        )
    if source_interval == "1d" and is_aggregated_interval(interval):
        candles = resample_candles(candles, interval)
    return candles[-900:] if range_name != "1d" else candles[-320:]


async def sina_candles(symbol_info: dict[str, str], range_name: str, interval: str) -> list[dict[str, Any]]:
    scale = normalize_interval(interval)
    source_interval = interval
    if scale in {"101", "102", "103"}:
        scale = "240"
        source_interval = "1d"
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
        f"?symbol={symbol_info['sina']}&scale={scale}&ma=no&datalen=1600"
    )
    raw = await http_json(
        url,
        ttl=6,
        key=f"sina:kline:{symbol_info['sina']}:{range_name}:{scale}",
        headers={"referer": "https://finance.sina.com.cn"},
    )
    cutoff = int(time.time() * 1000) - range_window_ms(range_name)
    candles = []
    for row in raw or []:
        timestamp = china_market_time_ms(row.get("day"))
        if timestamp is None:
            continue
        if range_name != "1d" and timestamp < cutoff:
            continue
        candles.append(
            {
                "time": timestamp,
                "open": finite(row.get("open")),
                "high": finite(row.get("high")),
                "low": finite(row.get("low")),
                "close": finite(row.get("close")),
                "volume": finite(row.get("volume")),
                "amount": finite(row.get("amount")),
            }
        )
    candles = [c for c in candles if all(c[k] is not None for k in ["open", "high", "low", "close"])]
    if source_interval == "1d" and is_aggregated_interval(interval):
        candles = resample_candles(candles, interval)
    return candles[-900:]


async def sina_quote(symbol_info: dict[str, str]) -> dict[str, Any]:
    text = await http_text(
        f"https://hq.sinajs.cn/list={symbol_info['sina']}",
        ttl=3,
        key=f"sina:{symbol_info['sina']}",
        headers={"referer": "https://finance.sina.com.cn"},
        encoding="gb18030",
    )
    match = re.search(r'="([^"]*)"', text)
    if not match:
        raise ValueError("No Sina quote data")
    fields = match.group(1).split(",")
    number = lambda index: finite(fields[index]) if index < len(fields) else None
    bids = [{"size": number(10 + i * 2), "price": number(11 + i * 2)} for i in range(5)]
    asks = [{"size": number(20 + i * 2), "price": number(21 + i * 2)} for i in range(5)]
    bids = [row for row in bids if row["price"] and row["size"] is not None]
    asks = [row for row in asks if row["price"] and row["size"] is not None]
    bid_total = sum(row["size"] for row in bids)
    ask_total = sum(row["size"] for row in asks)
    price = number(3)
    previous = number(2)
    change = price - previous if price is not None and previous is not None else None
    return {
        "name": fields[0] if fields else None,
        "quoteTime": f"{fields[30]} {fields[31]}".strip() if len(fields) > 31 else None,
        "quote": {
            "price": price,
            "previousClose": previous,
            "open": number(1),
            "dayHigh": number(4),
            "dayLow": number(5),
            "volume": number(8),
            "amount": number(9),
            "change": change,
            "changePercent": change / previous * 100 if change is not None and previous else None,
        },
        "orderBook": {
            "provider": "Sina Finance fallback",
            "note": "AKShare 暂不可用时使用新浪五档盘口兜底；免费源可能存在延迟。",
            "bids": bids,
            "asks": asks,
            "imbalance": (bid_total - ask_total) / max(bid_total + ask_total, 1),
        },
    }


def merge_realtime_daily_candle(candles: list[dict[str, Any]], quote: dict[str, Any], quote_time: str | None) -> list[dict[str, Any]]:
    if not candles or not quote_time:
        return candles
    today_ms = china_market_day_ms(quote_time)
    price = finite(quote.get("price"))
    if today_ms is None or price is None:
        return candles

    open_price = finite(quote.get("open")) or price
    high = finite(quote.get("dayHigh")) or max(open_price, price)
    low = finite(quote.get("dayLow")) or min(open_price, price)
    volume = finite(quote.get("volume"))
    amount = finite(quote.get("amount"))
    today_candle = {
        "time": today_ms,
        "open": open_price,
        "high": max(high, open_price, price),
        "low": min(low, open_price, price),
        "close": price,
        "volume": volume,
        "amount": amount,
        "realtime": True,
    }

    merged = [c for c in candles if int(c.get("time", 0)) != today_ms]
    merged.append(today_candle)
    return sorted(merged, key=lambda item: item["time"])


def same_china_week(left_ms: int, right_ms: int) -> bool:
    left = datetime.fromtimestamp(left_ms / 1000, CHINA_TZ)
    right = datetime.fromtimestamp(right_ms / 1000, CHINA_TZ)
    return left.isocalendar()[:2] == right.isocalendar()[:2]


def merge_realtime_weekly_candle(candles: list[dict[str, Any]], quote: dict[str, Any], quote_time: str | None) -> list[dict[str, Any]]:
    if not candles or not quote_time:
        return candles
    today_ms = china_market_day_ms(quote_time)
    price = finite(quote.get("price"))
    if today_ms is None or price is None:
        return candles

    latest = dict(candles[-1])
    if not same_china_week(int(latest.get("time", 0)), today_ms):
        latest = {
            "time": today_ms,
            "open": finite(quote.get("open")) or price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0,
            "amount": 0,
        }
        candles = [*candles, latest]

    latest["time"] = max(int(latest.get("time") or today_ms), today_ms)
    latest["high"] = max(finite(latest.get("high")) or price, finite(quote.get("dayHigh")) or price, price)
    latest["low"] = min(finite(latest.get("low")) or price, finite(quote.get("dayLow")) or price, price)
    latest["close"] = price
    latest["volume"] = max(finite(latest.get("volume")) or 0, finite(quote.get("volume")) or 0)
    latest["amount"] = max(finite(latest.get("amount")) or 0, finite(quote.get("amount")) or 0)
    latest["realtime"] = True

    merged = [*candles[:-1], latest]
    return sorted(merged, key=lambda item: item["time"])


async def fetch_a_share_market(symbol_info: dict[str, str], range_name: str, interval: str) -> dict[str, Any]:
    quote_source = "sina"
    kline_source = "eastmoney"
    fallback_notes = []

    try:
        quote_data = await sina_quote(symbol_info)
    except Exception as exc:
        fallback_notes.append(f"新浪报价失败：{exc}")
        quote_data = {
            "name": symbol_info["display"],
            "quoteTime": None,
            "quote": {},
            "orderBook": {
                "provider": "unavailable",
                "note": "盘口源暂不可用。",
                "bids": [],
                "asks": [],
                "imbalance": 0,
            },
        }
    name = quote_data.get("name")
    quote_time = quote_data.get("quoteTime")
    try:
        if range_name in {"5y", "10y", "all"} or is_aggregated_interval(interval):
            candles = await ak_minute_candles(symbol_info, range_name, interval)
            if len(candles) < 2:
                raise ValueError("AKShare returned insufficient candles")
            kline_source = "akshare"
        else:
            candles = await eastmoney_candles(symbol_info, range_name, interval)
    except Exception as exc:
        fallback_notes.append(f"东方财富K线失败，切换新浪K线：{exc}")
        try:
            candles = await sina_candles(symbol_info, range_name, interval)
            kline_source = "sina"
        except Exception as sina_exc:
            fallback_notes.append(f"新浪K线失败：{sina_exc}")
            candles = []

    if os.getenv("ENABLE_AKSHARE_ENRICH") == "1":
        try:
            ak_quote = await asyncio.wait_for(ak_bid_ask(symbol_info["code"]), timeout=2.0)
            if ak_quote.get("orderBook", {}).get("bids") and ak_quote.get("orderBook", {}).get("asks"):
                quote_data["orderBook"] = ak_quote["orderBook"]
                quote_data["quote"] = {**quote_data["quote"], **{k: v for k, v in ak_quote["quote"].items() if v is not None}}
                quote_source = "akshare"
        except Exception as exc:
            fallback_notes.append(f"AKShare盘口补充失败，继续使用新浪：{exc}")

        try:
            ak_candles = await asyncio.wait_for(ak_minute_candles(symbol_info, range_name, interval), timeout=2.0)
            if len(ak_candles) >= 20:
                candles = ak_candles
                kline_source = "akshare"
        except Exception as exc:
            fallback_notes.append(f"AKShare K线补充失败，继续使用当前K线源：{exc}")

    try:
        fund_flow = await a_share_fund_flow(symbol_info)
    except Exception as exc:
        fallback_notes.append(f"DDE资金流获取失败，使用盘口估算：{exc}")
        fund_flow = None

    quote = quote_data["quote"]
    if candles:
        quote["dayHigh"] = quote.get("dayHigh") or max(c["high"] for c in candles if c["high"] is not None)
        quote["dayLow"] = quote.get("dayLow") or min(c["low"] for c in candles if c["low"] is not None)
        quote["open"] = quote.get("open") or candles[0]["open"]
        quote["price"] = quote.get("price") or candles[-1]["close"]

    if interval == "1d":
        candles = merge_realtime_daily_candle(candles, quote, quote_time)
    elif interval in {"1wk", "1w", "1week"}:
        candles = merge_realtime_weekly_candle(candles, quote, quote_time)

    if fallback_notes:
        quote_data["orderBook"]["note"] = f"{quote_data['orderBook'].get('note', '')} {' '.join(fallback_notes)}".strip()

    return {
        "symbol": symbol_info["display"],
        "name": name,
        "exchange": "Shanghai Stock Exchange" if symbol_info["exchange"] == "sh" else "Shenzhen Stock Exchange",
        "currency": "CNY",
        "marketType": "cn",
        "marketState": "A股",
        "provider": f"AKShare/Pandas ({quote_source} quote, {kline_source} kline)",
        "delayed": True,
        "updatedAt": now_iso(),
        "quote": quote,
        "candles": candles,
        "orderBook": quote_data["orderBook"],
        "fundFlow": fund_flow,
        "quoteTime": quote_time,
    }


async def fetch_yahoo_market(symbol: str, range_name: str, interval: str) -> dict[str, Any]:
    yahoo_interval = {
        "1m": "1m",
        "2m": "2m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "60m": "60m",
        "1d": "1d",
        "1wk": "1wk",
        "1w": "1wk",
        "1week": "1wk",
        "1mo": "1mo",
        "3mo": "3mo",
        "6mo": "1mo",
    }.get(interval, "1d")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={yahoo_interval}&range={range_name}&includePrePost=true"
    )
    raw = await http_json(url, ttl=8, key=f"yahoo:{symbol}:{range_name}:{interval}")
    result = (raw.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise HTTPException(status_code=404, detail=f"No market data for {symbol}")
    meta = result.get("meta", {})
    timestamps = result.get("timestamp") or []
    quote_raw = (result.get("indicators", {}).get("quote") or [{}])[0]
    rows = []
    for index, timestamp in enumerate(timestamps):
        candle = {
            "time": timestamp * 1000,
            "open": finite((quote_raw.get("open") or [None])[index]),
            "high": finite((quote_raw.get("high") or [None])[index]),
            "low": finite((quote_raw.get("low") or [None])[index]),
            "close": finite((quote_raw.get("close") or [None])[index]),
            "volume": finite((quote_raw.get("volume") or [None])[index]),
        }
        if all(candle[key] is not None for key in ["open", "high", "low", "close"]):
            rows.append(candle)
    df = pd.DataFrame(rows)
    candles = df_records(df) if not df.empty else []
    if interval == "6mo":
        candles = resample_candles(candles, interval)
    previous = finite(meta.get("previousClose") or meta.get("chartPreviousClose"))
    price = finite(meta.get("regularMarketPrice")) or (candles[-1]["close"] if candles else None)
    change = price - previous if price is not None and previous else None
    return {
        "symbol": meta.get("symbol", symbol),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "Unknown",
        "currency": meta.get("currency", ""),
        "marketType": "us",
        "marketState": meta.get("marketState", "UNKNOWN"),
        "provider": "Yahoo Finance + Pandas",
        "delayed": True,
        "updatedAt": now_iso(),
        "quote": {
            "price": price,
            "previousClose": previous,
            "open": finite(meta.get("regularMarketDayOpen")) or (candles[0]["open"] if candles else None),
            "dayHigh": finite(meta.get("regularMarketDayHigh")) or (max(c["high"] for c in candles) if candles else None),
            "dayLow": finite(meta.get("regularMarketDayLow")) or (min(c["low"] for c in candles) if candles else None),
            "volume": finite(meta.get("regularMarketVolume")),
            "change": change,
            "changePercent": change / previous * 100 if change is not None and previous else None,
        },
        "candles": candles,
        "orderBook": synthesize_order_book(price, candles),
    }


def twelve_data_api_key() -> str | None:
    return os.getenv("TWELVE_DATA_API_KEY") or os.getenv("TWELVEDATA_API_KEY")


def moomoo_enabled() -> bool:
    return os.getenv("USE_MOOMOO", "0") == "1"


def moomoo_host() -> str:
    return os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")


def moomoo_port() -> int:
    return int(os.getenv("MOOMOO_OPEND_PORT", "11111"))


def moomoo_symbol(symbol: str) -> tuple[str, str, str]:
    raw = symbol.strip().upper()
    if raw.endswith(".HK"):
        code = raw.removesuffix(".HK").zfill(5)
        return f"HK.{code}", "hk", "HKD"
    return f"US.{raw}", "us", "USD"


def moomoo_ktype(api: Any, interval: str):
    mapping = {
        "1m": api.KLType.K_1M,
        "2m": api.KLType.K_1M,
        "5m": api.KLType.K_5M,
        "15m": api.KLType.K_15M,
        "30m": api.KLType.K_30M,
        "60m": api.KLType.K_60M,
        "1d": api.KLType.K_DAY,
        "1wk": api.KLType.K_WEEK,
        "1w": api.KLType.K_WEEK,
        "1week": api.KLType.K_WEEK,
        "1mo": api.KLType.K_MON,
        "3mo": api.KLType.K_MON,
        "6mo": api.KLType.K_MON,
    }
    return mapping.get(interval, api.KLType.K_DAY)


def moomoo_date_window(range_name: str) -> tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    if range_name == "all":
        start = end - timedelta(days=365 * 20)
    else:
        start = end - timedelta(milliseconds=range_window_ms(range_name))
    return start.isoformat(), end.isoformat()


def parse_moomoo_time(value: Any, market_type: str) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(CHINA_TZ if market_type == "hk" else US_TZ)
    return int(parsed.timestamp() * 1000)


def normalize_moomoo_frame(df: pd.DataFrame, market_type: str) -> list[dict[str, Any]]:
    rows = []
    for row in df_records(df):
        timestamp = parse_moomoo_time(row.get("time_key"), market_type)
        if timestamp is None:
            continue
        candle = {
            "time": timestamp,
            "open": finite(row.get("open")),
            "high": finite(row.get("high")),
            "low": finite(row.get("low")),
            "close": finite(row.get("close")),
            "volume": finite(row.get("volume")),
            "amount": finite(row.get("turnover")),
        }
        if all(candle[key] is not None for key in ["open", "high", "low", "close"]):
            rows.append(candle)
    return sorted(rows, key=lambda item: item["time"])


def moomoo_import():
    if not moomoo_enabled():
        raise RuntimeError("USE_MOOMOO is not 1.")
    try:
        import moomoo as mm
    except ImportError as exc:
        raise RuntimeError("moomoo-api is not installed. Run: pip install -r requirements.txt") from exc
    return mm


def moomoo_kline_ttl(range_name: str, interval: str) -> int:
    if interval in {"1m", "2m", "5m", "15m", "30m", "60m"}:
        return 8 if range_name in {"1d", "5d"} else 20
    return 60 if range_name in {"1d", "5d", "1mo"} else 180


def fetch_moomoo_candles_sync(symbol: str, range_name: str, interval: str) -> dict[str, Any]:
    mm = moomoo_import()
    code, market_type, default_currency = moomoo_symbol(symbol)
    quote_ctx = mm.OpenQuoteContext(host=moomoo_host(), port=moomoo_port())
    try:
        start, end = moomoo_date_window(range_name)
        ret, data, _ = quote_ctx.request_history_kline(
            code,
            start=start,
            end=end,
            ktype=moomoo_ktype(mm, interval),
            autype=mm.AuType.QFQ,
            max_count=1000,
        )
        if ret != mm.RET_OK:
            raise RuntimeError(str(data))
        candles = normalize_moomoo_frame(data, market_type)
        if interval in {"3mo", "6mo"}:
            candles = resample_candles(candles, interval)
        return {
            "code": code,
            "marketType": market_type,
            "currency": default_currency,
            "candles": candles,
        }
    finally:
        quote_ctx.close()


def fetch_moomoo_snapshot_sync(symbol: str) -> dict[str, Any]:
    mm = moomoo_import()
    code, market_type, default_currency = moomoo_symbol(symbol)
    quote_ctx = mm.OpenQuoteContext(host=moomoo_host(), port=moomoo_port())
    try:
        snapshot = {}
        ret, snap = quote_ctx.get_market_snapshot([code])
        if ret == mm.RET_OK and not snap.empty:
            snapshot = df_records(snap)[0]
        return {
            "code": code,
            "marketType": market_type,
            "currency": default_currency,
            "snapshot": snapshot,
        }
    finally:
        quote_ctx.close()


async def fetch_moomoo_candles(symbol: str, range_name: str, interval: str) -> dict[str, Any]:
    key = f"moomoo:kline:{symbol.upper()}:{range_name}:{interval}"
    ttl = moomoo_kline_ttl(range_name, interval)
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    payload = await run_blocking_timeout(12, fetch_moomoo_candles_sync, symbol, range_name, interval)
    return cache_set(key, payload, ttl)


async def fetch_moomoo_snapshot(symbol: str, ttl: float = 5) -> dict[str, Any]:
    key = f"moomoo:snapshot:{symbol.upper()}"
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    payload = await run_blocking_timeout(5, fetch_moomoo_snapshot_sync, symbol)
    return cache_set(key, payload, ttl)


async def fetch_moomoo_market(symbol: str, range_name: str, interval: str) -> dict[str, Any]:
    candle_payload, snapshot_payload = await asyncio.gather(
        fetch_moomoo_candles(symbol, range_name, interval),
        fetch_moomoo_snapshot(symbol),
    )
    candles = candle_payload.get("candles") or []
    snapshot = snapshot_payload.get("snapshot") or {}
    market_type = snapshot_payload.get("marketType") or candle_payload.get("marketType")
    currency = snapshot_payload.get("currency") or candle_payload.get("currency")

    price = finite(snapshot.get("last_price")) or (candles[-1]["close"] if candles else None)
    previous = finite(snapshot.get("prev_close_price"))
    change = price - previous if price is not None and previous else None
    return {
        "symbol": symbol.upper(),
        "name": snapshot.get("name"),
        "exchange": "Moomoo OpenD",
        "currency": currency,
        "marketType": market_type,
        "marketState": str(snapshot.get("sec_status") or "UNKNOWN"),
        "provider": "Moomoo OpenD + Pandas",
        "delayed": True,
        "updatedAt": now_iso(),
        "quoteTime": snapshot.get("update_time"),
        "quote": {
            "price": price,
            "previousClose": previous,
            "open": finite(snapshot.get("open_price")) or (candles[0]["open"] if candles else None),
            "dayHigh": finite(snapshot.get("high_price")) or (max(c["high"] for c in candles) if candles else None),
            "dayLow": finite(snapshot.get("low_price")) or (min(c["low"] for c in candles) if candles else None),
            "volume": finite(snapshot.get("volume")),
            "amount": finite(snapshot.get("turnover")),
            "change": change,
            "changePercent": change / previous * 100 if change is not None and previous else None,
        },
        "candles": candles,
        "orderBook": synthesize_order_book(price, candles),
    }


def twelve_data_symbol(symbol: str) -> tuple[str, str, str]:
    raw = symbol.strip().upper()
    if raw.endswith(".HK"):
        code = raw.removesuffix(".HK").zfill(4)
        return f"{code}:HKG", "hk", "HKD"
    return raw, "us", "USD"


def twelve_data_interval(interval: str) -> str:
    return {
        "1m": "1min",
        "2m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1h",
        "1d": "1day",
        "1wk": "1week",
        "1w": "1week",
        "1week": "1week",
        "1mo": "1month",
        "3mo": "1month",
        "6mo": "1month",
    }.get(interval, "1day")


def twelve_data_output_size(range_name: str, interval: str) -> int:
    if interval in {"1m", "2m"}:
        return 390 if range_name == "1d" else 1800
    if interval in {"5m", "15m", "30m", "60m"}:
        return 500
    return {
        "1d": 5,
        "5d": 10,
        "1mo": 32,
        "3mo": 80,
        "6mo": 150,
        "ytd": 260,
        "1y": 260,
        "3y": 800,
        "5y": 1300,
        "10y": 2600,
        "all": 5000,
    }.get(range_name, 260)


def parse_twelve_time(value: Any, market_type: str) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(CHINA_TZ if market_type == "hk" else US_TZ)
    return int(parsed.timestamp() * 1000)


async def fetch_twelve_data_market(symbol: str, range_name: str, interval: str) -> dict[str, Any]:
    api_key = twelve_data_api_key()
    if not api_key:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured.")

    td_symbol, market_type, default_currency = twelve_data_symbol(symbol)
    td_interval = twelve_data_interval(interval)
    output_size = twelve_data_output_size(range_name, interval)
    base_params = {"symbol": td_symbol, "apikey": api_key}

    quote_data, series_data = await asyncio.gather(
        http_json_params(
            "https://api.twelvedata.com/quote",
            params={**base_params},
            ttl=8,
            key=f"td:quote:{td_symbol}",
        ),
        http_json_params(
            "https://api.twelvedata.com/time_series",
            params={
                **base_params,
                "interval": td_interval,
                "outputsize": output_size,
                "order": "ASC",
            },
            ttl=8,
            key=f"td:series:{td_symbol}:{range_name}:{interval}",
        ),
    )

    values = series_data.get("values") or []
    candles = []
    for row in values:
        timestamp = parse_twelve_time(row.get("datetime"), market_type)
        if timestamp is None:
            continue
        candle = {
            "time": timestamp,
            "open": finite(row.get("open")),
            "high": finite(row.get("high")),
            "low": finite(row.get("low")),
            "close": finite(row.get("close")),
            "volume": finite(row.get("volume")),
        }
        if all(candle[key] is not None for key in ["open", "high", "low", "close"]):
            candles.append(candle)
    candles = sorted(candles, key=lambda item: item["time"])
    if interval in {"3mo", "6mo"}:
        candles = resample_candles(candles, interval)

    price = finite(quote_data.get("close")) or finite(quote_data.get("price")) or (candles[-1]["close"] if candles else None)
    previous = finite(quote_data.get("previous_close"))
    change = finite(quote_data.get("change"))
    if change is None and price is not None and previous:
        change = price - previous
    percent = finite(quote_data.get("percent_change"))
    exchange = quote_data.get("exchange") or ("Hong Kong Stock Exchange" if market_type == "hk" else "US")
    currency = quote_data.get("currency") or default_currency
    return {
        "symbol": symbol.upper(),
        "exchange": exchange,
        "currency": currency,
        "marketType": market_type,
        "marketState": "UNKNOWN",
        "provider": "Twelve Data + Pandas",
        "delayed": True,
        "updatedAt": now_iso(),
        "quoteTime": quote_data.get("datetime"),
        "quote": {
            "price": price,
            "previousClose": previous,
            "open": finite(quote_data.get("open")) or (candles[0]["open"] if candles else None),
            "dayHigh": finite(quote_data.get("high")) or (max(c["high"] for c in candles) if candles else None),
            "dayLow": finite(quote_data.get("low")) or (min(c["low"] for c in candles) if candles else None),
            "volume": finite(quote_data.get("volume")),
            "change": change,
            "changePercent": percent if percent is not None else (change / previous * 100 if change is not None and previous else None),
        },
        "candles": candles,
        "orderBook": synthesize_order_book(price, candles),
    }



# Local analysis has moved to backend.analysis. main.py keeps API routes,
# provider adapters, persistence, and third-party AI orchestration.


async def ai_analysis_blocking_legacy(payload: dict[str, Any]) -> dict[str, Any]:
    provider = os.getenv("AI_PROVIDER", "openai").lower()
    timeout = finite(os.getenv("THIRD_PARTY_AI_TIMEOUT")) or 20
    min_interval = finite(os.getenv("THIRD_PARTY_AI_MIN_INTERVAL")) or 300
    symbol = normalize_market_response_symbol(str(payload.get("symbol", "UNKNOWN")))
    range_name = str(payload.get("range") or "")
    interval = str(payload.get("interval") or "")
    key = f"ai:third_party:v5:seconds_macd:{provider}:{symbol}:{range_name}:{interval}"

    cached = cache_get(key, min_interval)
    if cached is not None:
        return {**cached, "cached": True, "aiReason": cached.get("aiReason") or f"第三方API低频缓存，{min_interval:.0f}s 内不重复请求。"}

    try:
        # 第三方AI独立做低频技术判断，避免拥塞时每轮行情刷新都打到外部API。
        if provider == "gemini":
            result = await asyncio.wait_for(gemini_seconds_macd_analysis(payload), timeout=timeout)
        else:
            result = await asyncio.wait_for(openai_seconds_macd_analysis(payload), timeout=timeout)
    except asyncio.TimeoutError:
        result = third_party_unavailable(provider, "timeout", f"第三方API超过 {timeout:.0f}s 未返回，已先展示本地分析。")

    return cache_set(key, result, min_interval)


async def request_third_party_ai(provider: str, payload: dict[str, Any], timeout: float, analysis_type: str) -> dict[str, Any]:
    try:
        # Third-party AI is an independent technical-analysis layer. Keep it bounded so local analysis can stay responsive.
        if provider == "gemini":
            return await asyncio.wait_for(gemini_third_party_analysis(payload, analysis_type), timeout=timeout)
        return await asyncio.wait_for(openai_third_party_analysis(payload, analysis_type), timeout=timeout)
    except asyncio.TimeoutError:
        return third_party_unavailable(provider, "timeout", f"第三方API超过 {timeout:.0f}s 未返回，已先展示本地分析。", analysis_type=analysis_type)


async def refresh_third_party_ai_cache(
    key: str,
    provider: str,
    payload: dict[str, Any],
    analysis_type: str,
    timeout: float,
    min_interval: float,
) -> None:
    try:
        try:
            result = await request_third_party_ai(provider, payload, timeout, analysis_type)
        except Exception as exc:
            request_logger.exception("THIRD_PARTY task failed type=%s provider=%s", analysis_type, provider)
            result = third_party_unavailable(provider, "failed", str(exc), analysis_type=analysis_type)
        cache_ttl = third_party_cache_ttl(analysis_type, result.get("aiStatus"))
        result = {**result, "cacheSavedAtEpoch": time.time(), "cacheTtlSeconds": cache_ttl}
        cache_set(key, result, cache_ttl)
        request_logger.info(
            "THIRD_PARTY cached type=%s provider=%s status=%s ttl=%.0fs",
            analysis_type,
            provider,
            result.get("aiStatus"),
            cache_ttl,
        )
    finally:
        third_party_ai_pending.discard(key)


def third_party_min_interval(analysis_type: str) -> float:
    if analysis_type == "micro":
        return finite(os.getenv("THIRD_PARTY_MICRO_MIN_INTERVAL")) or 30
    return finite(os.getenv("THIRD_PARTY_CHART_MIN_INTERVAL")) or finite(os.getenv("THIRD_PARTY_AI_MIN_INTERVAL")) or 300


def third_party_cache_ttl(analysis_type: str, status: str | None = None) -> float:
    if status and status != "ok":
        return finite(os.getenv("THIRD_PARTY_ERROR_CACHE_TTL")) or 15
    if analysis_type == "micro":
        return finite(os.getenv("THIRD_PARTY_MICRO_CACHE_TTL")) or 300
    return finite(os.getenv("THIRD_PARTY_CHART_CACHE_TTL")) or 1800


def third_party_error_retry_interval() -> float:
    return finite(os.getenv("THIRD_PARTY_ERROR_RETRY_INTERVAL")) or 15


def cached_result_age_seconds(result: dict[str, Any]) -> float | None:
    saved_at = finite(result.get("cacheSavedAtEpoch"))
    return time.time() - saved_at if saved_at else None


def schedule_third_party_refresh(
    key: str,
    provider: str,
    payload: dict[str, Any],
    analysis_type: str,
    timeout: float,
    min_interval: float,
) -> None:
    if key in third_party_ai_pending:
        return
    third_party_ai_pending.add(key)
    payload_copy = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    asyncio.create_task(refresh_third_party_ai_cache(key, provider, payload_copy, analysis_type, timeout, min_interval))


def third_party_cache_key(provider: str, analysis_type: str, symbol: str, range_name: str, interval: str) -> str:
    # Chart analysis is tied to the visible main chart period. Microstructure
    # analysis is tied to the symbol's realtime ticks/order book only, so chart
    # interval switches must not create a new third-party micro request.
    if analysis_type == "micro":
        return f"ai:third_party:v9:{analysis_type}:{provider}:{symbol}"
    return f"ai:third_party:v9:{analysis_type}:{provider}:{symbol}:{range_name}:{interval}"


async def ai_analysis_one(payload: dict[str, Any], analysis_type: str) -> dict[str, Any]:
    provider = os.getenv("AI_PROVIDER", "openai").lower()
    timeout = finite(os.getenv("THIRD_PARTY_AI_TIMEOUT")) or 20
    min_interval = third_party_min_interval(analysis_type)
    symbol = normalize_market_response_symbol(str(payload.get("symbol", "UNKNOWN")))
    range_name = str(payload.get("range") or "")
    interval = str(payload.get("interval") or "")
    key = third_party_cache_key(provider, analysis_type, symbol, range_name, interval)

    cached = cache_get(key, third_party_cache_ttl(analysis_type))
    if cached is not None:
        if (
            payload.get("enableThirdPartyAi") is not False
            and cached.get("aiStatus") not in {None, "ok", "pending"}
            and (cached_result_age_seconds(cached) is None or cached_result_age_seconds(cached) >= third_party_error_retry_interval())
        ):
            # Failed/timeout cache is only a short-lived display fallback. Keep
            # retrying in the background after a small cooldown so the UI can
            # recover without the user toggling the switch.
            schedule_third_party_refresh(key, provider, payload, analysis_type, timeout, min_interval)
        return {**cached, "cached": True, "aiReason": cached.get("aiReason") or f"第三方API低频缓存，{min_interval:.0f}s 内不重复请求。"}

    if payload.get("enableThirdPartyAi") is False:
        return third_party_unavailable(provider, "disabled", "第三方AI开关已关闭，且当前标的暂无缓存结果。", analysis_type=analysis_type)

    schedule_third_party_refresh(key, provider, payload, analysis_type, timeout, min_interval)

    return third_party_unavailable(provider, "pending", "第三方AI已转入后台低频请求，本地分析先返回；结果会在后续刷新中显示。", analysis_type=analysis_type)


async def ai_analysis(payload: dict[str, Any], heuristic: dict[str, Any] | None = None) -> dict[str, Any]:
    chart, micro = await asyncio.gather(
        ai_analysis_one(payload, "chart"),
        ai_analysis_one(payload, "micro"),
    )
    return {
        "chart": chart,
        "micro": micro,
        "aiProvider": chart.get("aiProvider") or micro.get("aiProvider"),
        "aiStatus": "ok" if chart.get("aiStatus") == "ok" or micro.get("aiStatus") == "ok" else chart.get("aiStatus") or micro.get("aiStatus"),
        "aiReason": "; ".join(filter(None, [chart.get("aiReason"), micro.get("aiReason")]))[:300],
    }


def third_party_unavailable(provider: str, status: str, reason: str, request_id: str | None = None, analysis_type: str = "unknown") -> dict[str, Any]:
    result = {
        "engine": provider,
        "aiProvider": provider,
        "aiStatus": status,
        "aiReason": reason,
        "available": False,
        "analysisType": analysis_type,
        "direction": None,
        "confidence": None,
        "action": None,
        "score": None,
        "signalLabel": None,
        "signalReasons": [],
        "opportunity30s": None,
        "icebergOrder": None,
        "institutionalBehavior": None,
        "summary": "",
        "reasons": [],
        "risks": [],
        "metrics": {},
        "signals": {},
        "kline": {},
    }
    if request_id:
        result["aiRequestId"] = request_id
    return result


def parse_ai_json(text: str | None) -> dict[str, Any]:
    if not text:
        raise ValueError("第三方AI没有返回文本内容。")
    source = text.strip()
    if source.startswith("```"):
        source = re.sub(r"^```(?:json)?\s*", "", source, flags=re.IGNORECASE).strip()
        source = re.sub(r"\s*```$", "", source).strip()
    start = source.find("{")
    end = source.rfind("}")
    if start >= 0 and end > start:
        source = source[start : end + 1]
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        preview = source[:240].replace("\n", " ")
        recovered = recover_partial_ai_json(source)
        if recovered:
            return {
                **recovered,
                "_parseWarning": f"第三方AI返回JSON不完整，已提取可用字段：{exc.msg}。片段：{preview}",
            }
        raise ValueError(f"第三方AI返回内容不是完整JSON：{exc.msg}。片段：{preview}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("第三方AI JSON 顶层不是对象。")
    return parsed


def recover_partial_ai_json(source: str) -> dict[str, Any] | None:
    # API congestion can truncate a JSON response after it already contains the
    # key decision fields. Recover those fields so the UI can show the useful
    # judgment instead of only "failed".
    def field_text(name: str) -> str | None:
        match = re.search(rf'"{re.escape(name)}"\s*:\s*"((?:\\.|[^"\\])*)"', source, re.S)
        if not match:
            return None
        return match.group(1).replace('\\"', '"').replace("\\n", "\n")

    def field_number(name: str) -> float | None:
        match = re.search(rf'"{re.escape(name)}"\s*:\s*(-?\d+(?:\.\d+)?)', source)
        return finite(match.group(1)) if match else None

    def array_text(name: str) -> list[str]:
        match = re.search(rf'"{re.escape(name)}"\s*:\s*\[(.*?)\]', source, re.S)
        if not match:
            return []
        return re.findall(r'"((?:\\.|[^"\\])*)"', match.group(1))[:3]

    direction = field_text("direction")
    summary = field_text("summary")
    action = field_text("action")
    signal_label = field_text("signalLabel")
    if not any([direction, summary, action, signal_label]):
        return None
    confidence = field_number("confidence")
    if confidence is not None and 0 <= confidence <= 1:
        confidence *= 100
    score = field_number("score")
    return {
        "direction": direction,
        "confidence": int(round(confidence)) if confidence is not None else None,
        "summary": summary or signal_label or action or "",
        "reasons": array_text("reasons") or array_text("signalReasons"),
        "risks": array_text("risks"),
        "action": action,
        "score": int(round(score)) if score is not None else None,
        "signalLabel": signal_label,
        "signalReasons": array_text("signalReasons"),
        "icebergOrder": {
            "detected": False,
            "side": "none",
            "confidence": 0,
            "evidence": "JSON截断，未提取到完整冰山字段",
        },
        "institutionalBehavior": {
            "classification": "未知",
            "confidence": 0,
            "evidence": "JSON截断，未提取到完整机构行为字段",
        },
        "opportunity30s": {
            "direction": direction or "震荡",
            "probability": int(round(confidence)) if confidence is not None else 0,
            "entryTrigger": "等待完整信号",
            "invalidation": "等待完整信号",
        },
    }


def compact_seconds_macd_payload(payload: dict[str, Any]) -> dict[str, Any]:
    realtime_ticks = payload.get("realtimeTicks") or []
    quote = payload.get("quote") or {}
    technical_snapshot: dict[str, Any] = {}
    if realtime_ticks:
        technical_snapshot = (build_seconds_macd_signal(realtime_ticks, quote) or {}).get("metrics") or {}

    return {
        "symbol": payload.get("symbol"),
        "marketType": payload.get("marketType"),
        "quote": payload.get("quote"),
        "orderBook": {
            "imbalance": (payload.get("orderBook") or {}).get("imbalance"),
            "provider": (payload.get("orderBook") or {}).get("provider"),
        },
        "recentCandles": (payload.get("candles") or [])[-24:],
        "realtimeTicks": realtime_ticks[-80:],
        "technicalSnapshot": technical_snapshot,
        "indicatorHints": {
            "mainChartCandleCount": len(payload.get("candles") or []),
            "realtimeTickCount": len(realtime_ticks),
        },
    }


def compact_chart_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candles = payload.get("candles") or []
    return {
        "symbol": payload.get("symbol"),
        "marketType": payload.get("marketType"),
        "range": payload.get("range"),
        "interval": payload.get("interval"),
        "quote": payload.get("quote"),
        "recentCandles": candles[-120:],
        "indicatorHints": {
            "candleCount": len(candles),
            "range": payload.get("range"),
            "interval": payload.get("interval"),
        },
    }


def estimate_order_flow(realtime_ticks: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    previous_price = None
    previous_volume = None
    buy_volume = 0.0
    sell_volume = 0.0
    neutral_volume = 0.0
    for tick in realtime_ticks[-240:]:
        price = finite(tick.get("price"))
        volume = finite(tick.get("volume"))
        if price is None:
            continue
        delta_volume = 0.0
        if volume is not None and previous_volume is not None:
            delta_volume = max(0.0, volume - previous_volume)
        direction = "neutral"
        if previous_price is not None:
            if price > previous_price:
                direction = "buy"
                buy_volume += delta_volume
            elif price < previous_price:
                direction = "sell"
                sell_volume += delta_volume
            else:
                neutral_volume += delta_volume
        rows.append({
            "time": tick.get("quoteTime") or tick.get("pushedAt") or tick.get("updatedAt"),
            "price": price,
            "volumeDelta": delta_volume,
            "direction": direction,
        })
        previous_price = price
        if volume is not None:
            previous_volume = volume
    total = buy_volume + sell_volume + neutral_volume
    return {
        "recentTrades": rows[-80:],
        "buyVolume": buy_volume,
        "sellVolume": sell_volume,
        "neutralVolume": neutral_volume,
        "orderFlowImbalance": (buy_volume - sell_volume) / total if total else None,
        "largePrintThreshold": max([row["volumeDelta"] for row in rows], default=0) * 0.7 if rows else None,
    }


def compact_micro_payload(payload: dict[str, Any]) -> dict[str, Any]:
    realtime_ticks = payload.get("realtimeTicks") or []
    order_book = payload.get("orderBook") or {}
    return {
        "symbol": payload.get("symbol"),
        "marketType": payload.get("marketType"),
        "quote": payload.get("quote"),
        "orderBook": {
            "provider": order_book.get("provider"),
            "imbalance": order_book.get("imbalance"),
            "bids": (order_book.get("bids") or [])[:10],
            "asks": (order_book.get("asks") or [])[:10],
        },
        "realtimeTicks": realtime_ticks[-160:],
        "orderFlow": estimate_order_flow(realtime_ticks),
        "task": "Identify iceberg orders, institutional microstructure behavior, liquidity absorption, spoofing/trap risk, and 30s opportunity.",
    }


def third_party_prompt(analysis_type: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if analysis_type == "chart":
        compact = compact_chart_payload(payload)
        return (
            "你是主图K线分析助手。你只分析主图 candles，对当前 range/interval 的趋势、支撑压力、MACD/量价/K线结构做判断。必须强调不构成投资建议。输出JSON。",
            "请基于主图K线独立判断，返回字段：direction, confidence, summary, reasons, risks, action, score, signalLabel, signalReasons。direction只能是偏多/偏空/震荡；action只能是 BUY/WAIT/SELL；summary不超过100字；reasons和risks最多3条。",
            compact,
        )
    compact = compact_micro_payload(payload)
    return (
        "你是市场微观结构分析助手。只分析实时ticks、盘口orderbook和委托流衍生特征，识别冰山订单、机构吸收/压单/撤单诱导、短线流动性失衡，并预测未来30秒交易机会。只输出短JSON，不写长段落。",
        (
            "返回严格JSON，字段必须完整且不得省略："
            "direction, confidence, summary, reasons, risks, action, score, signalLabel, signalReasons, "
            "icebergOrder, institutionalBehavior, opportunity30s。"
            "direction只能是偏多/偏空/震荡；confidence和score为0-100整数；"
            "summary不超过60字；reasons/risks/signalReasons各3条以内且每条20字以内；"
            "action只能是 BUY/WAIT/SELL/AVOID；signalLabel用2-6字短标签。"
            "icebergOrder必须是{detected:boolean, side:string, confidence:number, evidence:string}，side只能是bid/ask/none。"
            "institutionalBehavior必须是{classification:string, confidence:number, evidence:string}。"
            "opportunity30s必须是{direction:string, probability:number, entryTrigger:string, invalidation:string}。"
            "所有 evidence/trigger/invalidation 保持简洁但具体。"
        ),
        compact,
    )


async def openai_third_party_analysis(payload: dict[str, Any], analysis_type: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return third_party_unavailable("openai", "disabled", "OPENAI_API_KEY is not visible to FastAPI.", analysis_type=analysis_type)
    if os.getenv("USE_OPENAI", "1") != "1":
        return third_party_unavailable("openai", "disabled", "USE_OPENAI must be 1.", analysis_type=analysis_type)

    system_prompt, user_prompt, compact_payload = third_party_prompt(analysis_type, payload)
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "max_output_tokens": int(finite(os.getenv("OPENAI_MAX_OUTPUT_TOKENS")) or 2400),
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{user_prompt}\n{json.dumps(compact_payload, ensure_ascii=False)}"},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip()
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    try:
        request_timeout = finite(os.getenv("THIRD_PARTY_AI_TIMEOUT")) or 20
        timeout_config = httpx.Timeout(request_timeout, connect=3.0, read=request_timeout, write=3.0, pool=3.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json=body,
            )
        request_id = response.headers.get("x-request-id")
        if response.status_code >= 400:
            error = response.json().get("error", {}).get("message", response.text)
            return third_party_unavailable("openai", "failed", error, request_id, analysis_type=analysis_type)
        data = response.json()
        text = data.get("output_text")
        if not text:
            for item in data.get("output", []):
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text = part.get("text")
                        break
        parsed = parse_ai_json(text)
        parse_warning = parsed.pop("_parseWarning", None)
        return {
            **parsed,
            "engine": "openai",
            "aiStatus": "ok",
            "aiProvider": "openai",
            "aiReason": parse_warning,
            "aiRequestId": request_id,
            "available": True,
            "analysisType": analysis_type,
        }
    except Exception as exc:
        return third_party_unavailable("openai", "failed", str(exc), analysis_type=analysis_type)


async def openai_seconds_macd_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    return await openai_third_party_analysis(payload, "micro")


async def gemini_third_party_analysis(payload: dict[str, Any], analysis_type: str) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return third_party_unavailable("gemini", "disabled", "GEMINI_API_KEY is not visible to FastAPI.", analysis_type=analysis_type)

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    system_prompt, user_prompt, compact_payload = third_party_prompt(analysis_type, payload)
    prompt = f"{system_prompt}\n{user_prompt}\n{json.dumps(compact_payload, ensure_ascii=False)}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key, "content-type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
            )
        if response.status_code >= 400:
            error = response.json().get("error", {}).get("message", response.text)
            return third_party_unavailable("gemini", "failed", error, analysis_type=analysis_type)
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = parse_ai_json(text)
        parse_warning = parsed.pop("_parseWarning", None)
        return {
            **parsed,
            "engine": "gemini",
            "aiStatus": "ok",
            "aiProvider": "gemini",
            "aiReason": parse_warning,
            "available": True,
            "analysisType": analysis_type,
        }
    except Exception as exc:
        return third_party_unavailable("gemini", "failed", str(exc), analysis_type=analysis_type)


async def gemini_seconds_macd_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    return await gemini_third_party_analysis(payload, "micro")


@app.get("/api/health")
async def health():
    provider = os.getenv("AI_PROVIDER", "openai").lower()
    return {
        "ok": True,
        "runtime": "FastAPI",
        "data": {"akshare": True, "pandas": True},
        "features": {
            "akshareEnrich": os.getenv("ENABLE_AKSHARE_ENRICH") == "1",
        },
        "cache": {"memory": True, "redis": redis_status()},
        "storage": storage_status(),
        "ai": {
            "provider": provider,
            "openaiKeyVisible": bool(os.getenv("OPENAI_API_KEY")),
            "geminiKeyVisible": bool(os.getenv("GEMINI_API_KEY")),
            "twelveDataKeyVisible": bool(twelve_data_api_key()),
            "moomooEnabled": moomoo_enabled(),
            "moomooHost": moomoo_host(),
            "moomooPort": moomoo_port(),
            "ifindEnabled": ifind_enabled(),
            "ifindPushEnabled": ifind_push_enabled(),
            "ifindAccessTokenVisible": bool(os.getenv("IFIND_ACCESS_TOKEN")),
            "ifindRefreshTokenVisible": bool(os.getenv("IFIND_REFRESH_TOKEN")),
            "enabled": bool(os.getenv("OPENAI_API_KEY")) if provider == "openai" else bool(os.getenv("GEMINI_API_KEY")),
            "openaiModel": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            "geminiModel": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        },
    }


@app.get("/api/portfolio")
async def portfolio(market: str = "all"):
    return await build_portfolio_snapshot(market)


@app.post("/api/portfolio/trades")
async def add_portfolio_trade(payload: dict[str, Any]):
    symbol = str(payload.get("symbol", "")).strip()
    market = normalize_portfolio_market(payload.get("market"), symbol)
    side = str(payload.get("side", "buy")).lower()
    quantity = finite(payload.get("quantity"))
    price = finite(payload.get("price"))
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    if not quantity or quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be positive")
    if not price or price <= 0:
        raise HTTPException(status_code=400, detail="price must be positive")

    data = load_portfolio()
    trades = data.setdefault("trades", [])
    trade = {
        "id": payload.get("id") or f"T{int(time.time() * 1000)}",
        "market": market,
        "symbol": normalize_portfolio_symbol(symbol, market),
        "side": side,
        "quantity": quantity,
        "price": price,
        "time": payload.get("time") or now_iso(),
        "takeProfit": finite(payload.get("takeProfit")),
        "stopLoss": finite(payload.get("stopLoss")),
        "note": payload.get("note", ""),
    }
    trades.append(trade)
    save_portfolio(data)
    return await build_portfolio_snapshot(market)


@app.get("/api/market")
async def market(symbol: str = "600519", range: str = "1d", interval: str = "1m"):
    range = range.strip().lower()
    interval = interval.strip().lower()
    if range not in {"1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "3y", "5y", "10y", "all"}:
        raise HTTPException(status_code=400, detail="Invalid range")
    if interval not in {"1m", "2m", "5m", "15m", "30m", "60m", "1d", "1wk", "1w", "1week", "1mo", "3mo", "6mo"}:
        raise HTTPException(status_code=400, detail="Invalid interval")

    a_share = normalize_a_share_symbol(symbol)
    if a_share:
        try:
            payload = await fetch_china_market(a_share, range, interval)
            if not payload.get("candles"):
                raise RuntimeError("external China data source returned empty candles")
        except Exception as exc:
            payload = await fallback_to_local_market(
                market="cn",
                symbol=a_share["display"],
                range_name=range,
                interval=interval,
                reason=str(exc),
            )
        if not payload.get("cached"):
            schedule_market_candle_save(
                market=payload.get("marketType") or "cn",
                symbol=payload.get("symbol") or a_share["display"],
                interval=interval,
                provider=payload.get("provider") or "",
                candles=payload.get("candles") or [],
            )
        return payload
    if not re.match(r"^[A-Z0-9.\-=^]{1,20}$", symbol.upper()):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    normalized_symbol = symbol.upper()
    normalized_market = normalize_portfolio_market(None, normalized_symbol)
    try:
        payload = await fetch_moomoo_market(normalized_symbol, range, interval)
        if not payload.get("candles"):
            raise RuntimeError("Moomoo returned empty candles")
    except Exception as moomoo_exc:
        try:
            payload = await fetch_yahoo_market(normalized_symbol, range, interval)
            if not payload.get("candles"):
                raise RuntimeError("Yahoo returned empty candles")
            payload["provider"] = f"{payload['provider']} (Moomoo fallback: {moomoo_exc})"
            payload["orderBook"]["note"] = f"{payload['orderBook'].get('note', '')} Moomoo OpenD failed, fallback to Yahoo Finance.".strip()
        except Exception as yahoo_exc:
            try:
                payload = await fetch_twelve_data_market(normalized_symbol, range, interval)
                if not payload.get("candles"):
                    raise RuntimeError("Twelve Data returned empty candles")
                payload["provider"] = f"{payload['provider']} (Moomoo fallback: {moomoo_exc}; Yahoo fallback: {yahoo_exc})"
                payload["orderBook"]["note"] = f"{payload['orderBook'].get('note', '')} Moomoo OpenD and Yahoo Finance failed, fallback to Twelve Data.".strip()
            except Exception as twelve_exc:
                payload = await fallback_to_local_market(
                    market=normalized_market,
                    symbol=normalize_market_response_symbol(normalized_symbol),
                    range_name=range,
                    interval=interval,
                    reason=f"Moomoo failed: {moomoo_exc}; Yahoo failed: {yahoo_exc}; Twelve Data failed: {twelve_exc}",
                )
    if not payload.get("cached"):
        schedule_market_candle_save(
            market=payload.get("marketType") or normalize_portfolio_market(None, symbol),
            symbol=payload.get("symbol") or symbol.upper(),
            interval=interval,
            provider=payload.get("provider") or "",
            candles=payload.get("candles") or [],
        )
    return payload


@app.get("/api/realtime")
async def realtime_quote(symbol: str = "600519"):
    a_share = normalize_a_share_symbol(symbol)
    if a_share:
        pushed_quote = get_ifind_push_quote(a_share["display"])
        if pushed_quote:
            price = finite(pushed_quote.get("price"))
            if price is not None:
                return {
                    "symbol": a_share["display"],
                    "name": pushed_quote.get("name") or a_share["display"],
                    "marketType": "cn",
                    "currency": "CNY",
                    "provider": "iFinD Push",
                    "quoteTime": pushed_quote.get("quoteTime"),
                    "updatedAt": now_iso(),
                    "pushedAt": pushed_quote.get("pushedAt"),
                    "pushAgeSeconds": pushed_quote.get("ageSeconds"),
                    "price": price,
                    "change": finite(pushed_quote.get("change")),
                    "changePercent": finite(pushed_quote.get("changePercent")),
                    "volume": finite(pushed_quote.get("volume")),
                    "amount": finite(pushed_quote.get("amount")),
                }
        try:
            data = await ifind_realtime_quote(a_share, ttl=ifind_realtime_ttl())
            quote = data.get("quote") or {}
            price = finite(quote.get("price"))
            if price is None:
                raise RuntimeError("iFinD realtime quote has no price")
            return {
                "symbol": a_share["display"],
                "name": data.get("name") or a_share["display"],
                "marketType": "cn",
                "currency": "CNY",
                "provider": "iFinD realtime",
                "quoteTime": data.get("quoteTime"),
                "updatedAt": now_iso(),
                "price": price,
                "change": finite(quote.get("change")),
                "changePercent": finite(quote.get("changePercent")),
                "volume": finite(quote.get("volume")),
                "amount": finite(quote.get("amount")),
            }
        except Exception as ifind_exc:
            try:
                data = await sina_quote(a_share)
                quote = data.get("quote") or {}
                price = finite(quote.get("price"))
                if price is None:
                    raise RuntimeError("Sina realtime quote has no price")
                return {
                    "symbol": a_share["display"],
                    "name": data.get("name") or a_share["display"],
                    "marketType": "cn",
                    "currency": "CNY",
                    "provider": f"Sina realtime fallback (iFinD: {ifind_exc})",
                    "quoteTime": data.get("quoteTime"),
                    "updatedAt": now_iso(),
                    "price": price,
                    "change": finite(quote.get("change")),
                    "changePercent": finite(quote.get("changePercent")),
                    "volume": finite(quote.get("volume")),
                    "amount": finite(quote.get("amount")),
                }
            except Exception as sina_exc:
                cached = await local_cached_market_payload(
                    market="cn",
                    symbol=a_share["display"],
                    range_name="1d",
                    interval="1d",
                    reason=f"iFinD realtime failed: {ifind_exc}; Sina realtime failed: {sina_exc}",
                )
                if cached:
                    quote = cached.get("quote") or {}
                    return {
                        "symbol": cached.get("symbol") or a_share["display"],
                        "name": cached.get("name") or a_share["display"],
                        "marketType": "cn",
                        "currency": "CNY",
                        "provider": cached.get("provider") or "Local SQLite realtime cache",
                        "quoteTime": cached.get("quoteTime"),
                        "updatedAt": now_iso(),
                        "cached": True,
                        "price": finite(quote.get("price")),
                        "change": finite(quote.get("change")),
                        "changePercent": finite(quote.get("changePercent")),
                        "volume": finite(quote.get("volume")),
                        "amount": finite(quote.get("amount")),
                    }
                raise HTTPException(status_code=502, detail=f"realtime quote unavailable: {ifind_exc}; {sina_exc}")

    normalized_symbol = symbol.strip().upper()
    if re.match(r"^\d{1,5}$", normalized_symbol):
        raise HTTPException(status_code=400, detail="Incomplete A-share symbol")
    if not re.match(r"^[A-Z0-9.\-=^]{1,20}$", normalized_symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    try:
        snapshot_payload = await fetch_moomoo_snapshot(normalized_symbol, ttl=1)
        snapshot = snapshot_payload.get("snapshot") or {}
        price = finite(snapshot.get("last_price"))
        previous = finite(snapshot.get("prev_close_price"))
        change = price - previous if price is not None and previous else None
        if price is None:
            raise RuntimeError("Moomoo snapshot has no price")
        return {
            "symbol": normalized_symbol,
            "name": snapshot.get("name"),
            "marketType": snapshot_payload.get("marketType") or normalize_portfolio_market(None, normalized_symbol),
            "currency": snapshot_payload.get("currency"),
            "provider": "Moomoo OpenD snapshot",
            "quoteTime": snapshot.get("update_time"),
            "updatedAt": now_iso(),
            "price": price,
            "change": change,
            "changePercent": change / previous * 100 if change is not None and previous else None,
            "volume": finite(snapshot.get("volume")),
            "amount": finite(snapshot.get("turnover")),
        }
    except Exception as exc:
        market_type = normalize_portfolio_market(None, normalized_symbol)
        cached = await local_cached_market_payload(
            market=market_type,
            symbol=normalize_market_response_symbol(normalized_symbol),
            range_name="1d",
            interval="1d",
            reason=f"Moomoo realtime failed: {exc}",
        )
        if cached:
            quote = cached.get("quote") or {}
            return {
                "symbol": cached.get("symbol") or normalized_symbol,
                "name": cached.get("name") or normalized_symbol,
                "marketType": cached.get("marketType") or market_type,
                "currency": cached.get("currency"),
                "provider": cached.get("provider") or "Local SQLite realtime cache",
                "quoteTime": cached.get("quoteTime"),
                "updatedAt": now_iso(),
                "cached": True,
                "price": finite(quote.get("price")),
                "change": finite(quote.get("change")),
                "changePercent": finite(quote.get("changePercent")),
                "volume": finite(quote.get("volume")),
                "amount": finite(quote.get("amount")),
            }
        raise HTTPException(status_code=502, detail=f"realtime quote unavailable: {exc}") from exc


@app.get("/api/ifind/push/ticks")
async def ifind_push_ticks(symbol: str = "600519", limit: int = 200):
    a_share = normalize_a_share_symbol(symbol)
    if not a_share:
        raise HTTPException(status_code=400, detail="Only A-share symbols are supported")
    ticks = get_ifind_push_ticks(a_share["display"], limit=limit)
    latest = get_ifind_push_quote(a_share["display"])
    latest_tick = ticks[-1] if ticks else None
    latest_pushed_at = parse_time(latest_tick.get("pushedAt")) if latest_tick else None
    latest_age = (
        (datetime.now(timezone.utc) - latest_pushed_at.astimezone(timezone.utc)).total_seconds()
        if latest_pushed_at
        else None
    )
    return {
        "symbol": a_share["display"],
        "enabled": ifind_push_enabled(),
        "count": len(ticks),
        "latest": latest,
        "latestTickAgeSeconds": finite(latest_age),
        "oldestTickTime": ticks[0].get("quoteTime") if ticks else None,
        "newestTickTime": latest_tick.get("quoteTime") if latest_tick else None,
        "provider": latest_tick.get("provider") if latest_tick else "iFinD Push",
        "ticks": ticks,
    }


@app.post("/api/analyze")
async def analyze(request: Request):
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except UnicodeDecodeError:
        payload = json.loads(raw_body.decode("gb18030", errors="replace"))
    heuristic = run_local_analysis(
        payload.get("symbol", "UNKNOWN"),
        payload.get("quote") or {},
        payload.get("candles") or [],
        payload.get("orderBook") or {},
        payload.get("fundFlow") or None,
        payload.get("realtimeTicks") or [],
        str(payload.get("range") or ""),
        str(payload.get("interval") or ""),
    )
    third_party = await ai_analysis(payload, heuristic)
    response = {
        **heuristic,
        "engine": "split",
        "local": heuristic,
        "thirdParty": third_party,
        "aiStatus": third_party.get("aiStatus"),
        "aiReason": third_party.get("aiReason"),
        "aiProvider": third_party.get("aiProvider"),
    }
    saved_id = await asyncio.to_thread(
        save_ai_analysis,
        market=normalize_portfolio_market(payload.get("marketType"), str(payload.get("symbol", ""))),
        symbol=normalize_market_response_symbol(str(payload.get("symbol", "UNKNOWN"))),
        range_name=str(payload.get("range") or ""),
        interval=str(payload.get("interval") or ""),
        local=heuristic,
        third_party=third_party,
    )
    response["historyId"] = saved_id
    return response


@app.get("/api/history/candles")
async def history_candles(market: str | None = None, symbol: str | None = None, interval: str | None = None, limit: int = 500):
    normalized_market = normalize_portfolio_market(market) if market else None
    normalized_symbol = normalize_market_response_symbol(symbol) if symbol else None
    return {
        "items": await asyncio.to_thread(
            list_market_candles,
            market=normalized_market,
            symbol=normalized_symbol,
            interval=interval,
            limit=limit,
        )
    }


@app.get("/api/history/analysis")
async def history_analysis(market: str | None = None, symbol: str | None = None, limit: int = 100):
    normalized_market = normalize_portfolio_market(market) if market else None
    normalized_symbol = normalize_market_response_symbol(symbol) if symbol else None
    return {
        "items": await asyncio.to_thread(
            list_ai_analysis,
            market=normalized_market,
            symbol=normalized_symbol,
            limit=limit,
        )
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.exception_handler(Exception)
async def all_exception_handler(_: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
