import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.cache_store import redis_get, redis_set, redis_status
from backend.db import init_db, list_ai_analysis, list_market_candles, save_ai_analysis, save_market_candles, storage_status


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "public"
PORTFOLIO_FILE = ROOT / "data" / "portfolio.json"
PORT = int(os.getenv("PORT", "5177"))
CHINA_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")
PORTFOLIO_MARKETS = {
    "cn": {"label": "中国", "currency": "CNY"},
    "us": {"label": "美国", "currency": "USD"},
    "hk": {"label": "香港", "currency": "HKD"},
}

app = FastAPI(title="L2LLM Stock AI", version="0.2.0")
cache: dict[str, tuple[float, Any]] = {}


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
    saved_at, value = hit
    if time.time() - saved_at > ttl:
        return None
    return value


def cache_set(key: str, value: Any, ttl: float = 30) -> Any:
    cache[key] = (time.time(), value)
    redis_set(key, value, ttl)
    return value


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def china_market_time_ms(value: Any) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(CHINA_TZ)
    else:
        parsed = parsed.tz_convert(CHINA_TZ)
    return int(parsed.timestamp() * 1000)


def china_market_day_ms(value: Any) -> int | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(CHINA_TZ)
    else:
        parsed = parsed.tz_convert(CHINA_TZ)
    day_start = parsed.normalize()
    return int(day_start.timestamp() * 1000)


def normalize_a_share_symbol(symbol: str) -> dict[str, str] | None:
    raw = symbol.strip().upper().replace(" ", "")
    prefixed = re.match(r"^(SH|SZ)(\d{6})$", raw)
    suffixed = re.match(r"^(\d{6})\.(SH|SZ|SS)$", raw)
    plain = re.match(r"^(\d{6})$", raw)

    code = None
    exchange = None
    if prefixed:
        exchange = prefixed.group(1).lower()
        code = prefixed.group(2)
    elif suffixed:
        code = suffixed.group(1)
        exchange = "sh" if suffixed.group(2) in {"SH", "SS"} else "sz"
    elif plain:
        code = plain.group(1)
        exchange = "sh" if code.startswith(("6", "9")) else "sz"

    if not code or not exchange:
        return None

    return {
        "code": code,
        "exchange": exchange,
        "display": f"{exchange.upper()}{code}",
        "secid": f"{1 if exchange == 'sh' else 0}.{code}",
        "sina": f"{exchange}{code}",
    }


def normalize_market_response_symbol(symbol: str) -> str:
    a_share = normalize_a_share_symbol(symbol)
    if a_share:
        return a_share["display"]
    return symbol.strip().upper()


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


def range_window_ms(range_name: str) -> int:
    if range_name == "all":
        return 100 * 365 * 24 * 60 * 60 * 1000
    if range_name == "ytd":
        start = datetime(datetime.now().year, 1, 1)
        return max(1, int((datetime.now() - start).total_seconds() * 1000))
    days = {
        "1d": 1,
        "5d": 7,
        "1mo": 35,
        "3mo": 100,
        "6mo": 190,
        "1y": 380,
        "3y": 365 * 3 + 15,
        "5y": 365 * 5 + 20,
        "10y": 365 * 10 + 40,
    }.get(range_name, 1)
    return days * 24 * 60 * 60 * 1000


def normalize_interval(interval: str) -> str:
    return {
        "1m": "1",
        "2m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1d": "101",
        "1wk": "102",
        "1w": "102",
        "1week": "102",
        "1mo": "103",
        "3mo": "103",
        "6mo": "103",
    }.get(interval, "1")


def is_aggregated_interval(interval: str) -> bool:
    return interval in {"1wk", "1w", "1week", "1mo", "3mo", "6mo"}


def resample_candles(candles: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    if not is_aggregated_interval(interval) or not candles:
        return candles

    df = pd.DataFrame(candles)
    if df.empty:
        return candles

    df["datetime"] = pd.to_datetime(df["time"], unit="ms", errors="coerce", utc=True).dt.tz_convert(CHINA_TZ)
    df = df.dropna(subset=["datetime"]).sort_values("datetime").set_index("datetime")
    df["time_ms"] = [int(timestamp.timestamp() * 1000) for timestamp in df.index]
    rule = {
        "1wk": "W-FRI",
        "1w": "W-FRI",
        "1week": "W-FRI",
        "1mo": "ME",
        "3mo": "QE",
        "6mo": "2QE",
    }[interval]

    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
        "time_ms": "last",
    }
    resampled = df.resample(rule).agg(agg).dropna(subset=["open", "high", "low", "close"])
    records = []
    for timestamp, row in resampled.iterrows():
        records.append(
            {
                "time": int(row.get("time_ms")),
                "open": finite(row.get("open")),
                "high": finite(row.get("high")),
                "low": finite(row.get("low")),
                "close": finite(row.get("close")),
                "volume": finite(row.get("volume")),
                "amount": finite(row.get("amount")),
            }
        )
    return records


async def http_json(url: str, *, ttl: float, key: str, headers: dict[str, str] | None = None) -> Any:
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        response = await client.get(url)
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ").strip()
            raise RuntimeError(f"HTTP {response.status_code} from data source: {url}. {body}")
        return cache_set(key, response.json(), ttl)


async def http_text(url: str, *, ttl: float, key: str, headers: dict[str, str] | None = None, encoding: str = "utf-8") -> str:
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        response = await client.get(url)
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ").strip()
            raise RuntimeError(f"HTTP {response.status_code} from data source: {url}. {body}")
        text = response.content.decode(encoding, errors="replace")
        return cache_set(key, text, ttl)


async def http_json_params(url: str, *, params: dict[str, Any], ttl: float, key: str) -> Any:
    cached = cache_get(key, ttl)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.get(url, params=params)
        if response.status_code >= 400:
            body = response.text[:300].replace("\n", " ").strip()
            raise RuntimeError(f"HTTP {response.status_code} from data source: {response.url}. {body}")
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error: {data.get('message') or data.get('code') or data}")
        return cache_set(key, data, ttl)


def ifind_enabled() -> bool:
    return os.getenv("USE_IFIND") == "1"


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

    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.post(
            "https://quantapi.51ifind.com/api/v1/get_access_token",
            headers={"Content-Type": "application/json", "refresh_token": refresh_token},
        )
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
    async with httpx.AsyncClient(timeout=16.0) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "access_token": token, "Accept-Encoding": "gzip,deflate"},
        )
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


def df_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(df.where(pd.notnull(df), None).to_json(orient="records", force_ascii=False))


def numeric_series(df: pd.DataFrame, column: str, default: float = 0) -> pd.Series:
    if column in df:
        source = df[column]
    else:
        source = pd.Series([default] * len(df), index=df.index)
    return pd.to_numeric(source, errors="coerce").fillna(default)


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

    quote_data, candles = await asyncio.gather(
        ifind_realtime_quote(symbol_info),
        ifind_candles(symbol_info, range_name, interval),
    )
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

    try:
        order_book = (await sina_quote(symbol_info))["orderBook"]
        order_book["note"] = f"{order_book.get('note', '')} iFinD行情优先，盘口使用新浪五档兜底。".strip()
    except Exception:
        order_book = synthesize_order_book(finite(quote.get("price")), candles)
        order_book["note"] = "iFinD行情优先；盘口源不可用，当前使用估算盘口。"

    try:
        fund_flow = await a_share_fund_flow(symbol_info)
    except Exception:
        fund_flow = None

    return {
        "symbol": symbol_info["display"],
        "name": quote_data.get("name") or symbol_info["display"],
        "exchange": "Shanghai Stock Exchange" if symbol_info["exchange"] == "sh" else "Shenzhen Stock Exchange",
        "currency": "CNY",
        "marketType": "cn",
        "marketState": "A股",
        "provider": "iFinD HTTP API + Pandas",
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


async def local_cached_market_payload(
    *,
    market: str,
    symbol: str,
    range_name: str,
    interval: str,
    reason: str,
) -> dict[str, Any] | None:
    rows = await asyncio.to_thread(
        list_market_candles,
        market=market,
        symbol=symbol,
        interval=interval,
        limit=5000,
    )
    if not rows and is_aggregated_interval(interval):
        daily_rows = await asyncio.to_thread(
            list_market_candles,
            market=market,
            symbol=symbol,
            interval="1d",
            limit=5000,
        )
        daily_candles = [
            {
                "time": row.get("time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "amount": None,
            }
            for row in daily_rows
        ]
        rows = [
            {
                "time": candle.get("time"),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "volume": candle.get("volume"),
                "provider": "local daily resample",
            }
            for candle in resample_candles(daily_candles, interval)
        ]
    if not rows:
        return None

    cutoff = int(time.time() * 1000) - range_window_ms(range_name)
    candles = [
        {
            "time": row.get("time"),
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "cached": True,
        }
        for row in rows
        if range_name == "all" or int(row.get("time") or 0) >= cutoff
    ]
    if not candles:
        candles = [
            {
                "time": row.get("time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "cached": True,
            }
            for row in rows[-min(len(rows), 500):]
        ]
    candles = [candle for candle in candles if all(candle.get(key) is not None for key in ["time", "open", "high", "low", "close"])]
    if not candles:
        return None

    last = candles[-1]
    previous = candles[-2]["close"] if len(candles) > 1 else last["open"]
    price = finite(last.get("close"))
    previous_close = finite(previous)
    change = price - previous_close if price is not None and previous_close is not None else None
    currency = {"cn": "CNY", "us": "USD", "hk": "HKD"}.get(market, "")
    exchange = {
        "cn": "China A-share cached history",
        "us": "US cached history",
        "hk": "Hong Kong cached history",
    }.get(market, "cached history")

    quote = {
        "price": price,
        "previousClose": previous_close,
        "open": finite(last.get("open")),
        "dayHigh": finite(last.get("high")),
        "dayLow": finite(last.get("low")),
        "volume": finite(last.get("volume")),
        "amount": None,
        "change": change,
        "changePercent": change / previous_close * 100 if change is not None and previous_close else None,
    }
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": exchange,
        "currency": currency,
        "marketType": market,
        "marketState": "LOCAL_CACHE",
        "provider": f"Local SQLite cache (external fallback: {reason})",
        "delayed": True,
        "cached": True,
        "updatedAt": now_iso(),
        "quote": quote,
        "candles": candles,
        "orderBook": {
            **synthesize_order_book(price, candles),
            "note": "外部数据源不可用，当前读取本地 SQLite 历史K线；盘口为估算值。",
        },
        "fundFlow": None,
        "quoteTime": datetime.fromtimestamp(int(last["time"]) / 1000, timezone.utc).isoformat(),
    }


async def fallback_to_local_market(
    *,
    market: str,
    symbol: str,
    range_name: str,
    interval: str,
    reason: str,
) -> dict[str, Any]:
    cached = await local_cached_market_payload(
        market=market,
        symbol=symbol,
        range_name=range_name,
        interval=interval,
        reason=reason,
    )
    if cached:
        return cached
    raise HTTPException(status_code=502, detail=f"{reason}; local SQLite cache is empty for {symbol} {interval}.")


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


def synthesize_order_book(price: float | None, candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not price:
        return {"provider": "synthetic-depth", "bids": [], "asks": [], "imbalance": 0}
    df = pd.DataFrame(candles[-30:])
    avg_range = float((df["high"] - df["low"]).clip(lower=0).mean()) if not df.empty else price * 0.001
    tick = max(price * 0.0003, avg_range * 0.08, 0.01)
    momentum = 0.0
    if len(candles) > 5:
        momentum = (candles[-1]["close"] - candles[-5]["close"]) / candles[-5]["close"]
    bias = max(-0.35, min(0.35, momentum * 12))
    bids = [{"price": price - tick * i, "size": round((1200 + i * 170) * (1 + bias))} for i in range(1, 9)]
    asks = [{"price": price + tick * i, "size": round((1200 + i * 170) * (1 - bias))} for i in range(1, 9)]
    bid_total = sum(row["size"] for row in bids)
    ask_total = sum(row["size"] for row in asks)
    return {
        "provider": "synthetic-depth",
        "note": "美股免费源不含实时Level2盘口，此处为演示盘口；A股会优先使用 AKShare 五档盘口。",
        "bids": bids,
        "asks": asks,
        "imbalance": (bid_total - ask_total) / max(bid_total + ask_total, 1),
    }


def compute_indicators(candles: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    if df.empty:
        return {"ma20": None, "ma60": None, "rsi14": None, "volumeRatio": 1, "support": None, "resistance": None}
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = numeric_series(df, "volume")
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    recent_volume = volume.tail(5).mean()
    base_volume = volume.iloc[-40:-5].mean() if len(volume) > 40 else volume.mean()
    recent = df.tail(20)
    return {
        "ma20": finite(close.tail(20).mean()) if len(close) >= 20 else None,
        "ma60": finite(close.tail(60).mean()) if len(close) >= 60 else None,
        "rsi14": finite(rsi.iloc[-1]) if not rsi.empty else None,
        "volumeRatio": finite(recent_volume / base_volume) if base_volume else 1,
        "support": finite(pd.to_numeric(recent["low"], errors="coerce").min()) if not recent.empty else None,
        "resistance": finite(pd.to_numeric(recent["high"], errors="coerce").max()) if not recent.empty else None,
    }


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def label_by_score(score: float, high: str = "高", mid: str = "中", low: str = "低") -> str:
    if score >= 70:
        return high
    if score >= 45:
        return mid
    return low


def estimate_dde_flow(
    quote: dict[str, Any],
    candles: list[dict[str, Any]],
    order_book: dict[str, Any],
) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    amount = numeric_series(df, "amount") if not df.empty else pd.Series(dtype="float64")
    volume = numeric_series(df, "volume") if not df.empty else pd.Series(dtype="float64")
    last_amount = finite(quote.get("amount")) or (finite(amount.iloc[-1]) if not amount.empty else 0) or 0
    avg_amount = finite(amount.tail(20).mean()) if not amount.empty else 0
    turnover = last_amount or avg_amount or 0
    change_pct = finite(quote.get("changePercent")) or 0
    order_imbalance = finite(order_book.get("imbalance")) or 0
    volume_ratio = 1
    if len(volume) >= 20:
        recent_volume = finite(volume.tail(5).mean()) or 0
        base_volume = finite(volume.iloc[-20:-5].mean()) or 0
        volume_ratio = recent_volume / base_volume if base_volume else 1

    flow_ratio = clamp(order_imbalance * 18 + change_pct * 0.9 + (volume_ratio - 1) * 6, -25, 25)
    main_net = turnover * flow_ratio / 100 if turnover else 0
    return {
        "source": "order-book and turnover estimate",
        "quality": "estimated",
        "estimated": True,
        "date": now_iso(),
        "mainNetInflow": main_net,
        "mainNetInflowRatio": flow_ratio,
        "superLargeNetInflow": main_net * 0.45,
        "superLargeNetInflowRatio": flow_ratio * 0.45,
        "largeNetInflow": main_net * 0.35,
        "largeNetInflowRatio": flow_ratio * 0.35,
        "mediumNetInflow": -main_net * 0.2,
        "mediumNetInflowRatio": -flow_ratio * 0.2,
        "smallNetInflow": -main_net * 0.6,
        "smallNetInflowRatio": -flow_ratio * 0.6,
        "ddx": flow_ratio,
        "ddy": flow_ratio * max(0.6, min(1.8, volume_ratio)),
        "ddz": flow_ratio + order_imbalance * 30,
        "recentMainNetInflow": main_net,
        "recentMainNetInflowRatioAvg": flow_ratio,
    }


def build_dde_signal(fund_flow: dict[str, Any] | None) -> dict[str, Any]:
    flow = fund_flow or {}
    ddx = finite(flow.get("ddx")) or 0
    ddy = finite(flow.get("ddy")) or 0
    ddz = finite(flow.get("ddz")) or 0
    main_ratio = finite(flow.get("mainNetInflowRatio")) or ddx
    main_net = finite(flow.get("mainNetInflow")) or 0
    super_ratio = finite(flow.get("superLargeNetInflowRatio")) or 0
    large_ratio = finite(flow.get("largeNetInflowRatio")) or 0

    score = 50 + main_ratio * 2.2 + ddy * 1.2 + ddz * 0.8
    if super_ratio + large_ratio > 0:
        score += min(12, (super_ratio + large_ratio) * 0.8)
    else:
        score += max(-12, (super_ratio + large_ratio) * 0.8)
    score = clamp(score)

    if main_ratio >= 5 and ddz >= 5:
        label = "强流入"
    elif main_ratio >= 1.5:
        label = "流入"
    elif main_ratio <= -5 and ddz <= -5:
        label = "强流出"
    elif main_ratio <= -1.5:
        label = "流出"
    else:
        label = "均衡"

    reasons = [
        f"主力净流入占比 {main_ratio:.2f}%",
        f"DDX {ddx:.2f}，DDY {ddy:.2f}，DDZ {ddz:.2f}",
    ]
    if main_net:
        reasons.append(f"主力净额约 {main_net / 10000:.1f} 万")
    if flow.get("estimated"):
        reasons.append("缺少逐笔大单数据，当前为盘口/成交额估算")
    elif flow.get("source"):
        reasons.append(f"资金流来源：{flow.get('source')}")

    return {
        "score": round(score),
        "label": label,
        "reasons": reasons[:5],
        "metrics": {
            "ddx": ddx,
            "ddy": ddy,
            "ddz": ddz,
            "mainNetInflow": main_net,
            "mainNetInflowRatio": main_ratio,
            "superLargeNetInflowRatio": super_ratio,
            "largeNetInflowRatio": large_ratio,
            "estimated": bool(flow.get("estimated")),
            "source": flow.get("source") or "",
        },
    }


def build_realtime_bars(ticks: list[dict[str, Any]], interval_ms: int = 3000) -> list[dict[str, Any]]:
    rows = []
    for tick in ticks or []:
        timestamp = finite(tick.get("time"))
        price = finite(tick.get("price"))
        if timestamp is None or price is None:
            continue
        rows.append(
            {
                "time": int(timestamp),
                "price": price,
                "volume": finite(tick.get("volume")),
                "amount": finite(tick.get("amount")),
            }
        )
    rows = sorted(rows, key=lambda item: item["time"])
    if not rows:
        return []

    normalized = []
    previous_volume = None
    previous_amount = None
    for row in rows:
        volume = row.get("volume")
        amount = row.get("amount")
        delta_volume = 0
        delta_amount = 0
        if volume is not None and previous_volume is not None:
            delta_volume = max(0, volume - previous_volume)
        if amount is not None and previous_amount is not None:
            delta_amount = max(0, amount - previous_amount)
        normalized.append({**row, "deltaVolume": delta_volume, "deltaAmount": delta_amount})
        if volume is not None:
            previous_volume = volume
        if amount is not None:
            previous_amount = amount

    bars = []
    for row in normalized:
        bucket = row["time"] - row["time"] % interval_ms
        if not bars or bars[-1]["time"] != bucket:
            bars.append(
                {
                    "time": bucket,
                    "open": row["price"],
                    "high": row["price"],
                    "low": row["price"],
                    "close": row["price"],
                    "volume": row["deltaVolume"],
                    "amount": row["deltaAmount"],
                }
            )
            continue
        bar = bars[-1]
        bar["high"] = max(bar["high"], row["price"])
        bar["low"] = min(bar["low"], row["price"])
        bar["close"] = row["price"]
        bar["volume"] += row["deltaVolume"]
        bar["amount"] += row["deltaAmount"]

    for bar in bars:
        if not bar["volume"]:
            bar["volume"] = 1
        if not bar["amount"]:
            bar["amount"] = bar["close"] * bar["volume"]
    return bars


def build_seconds_macd_signal(realtime_ticks: list[dict[str, Any]], quote: dict[str, Any]) -> dict[str, Any]:
    bars = build_realtime_bars(realtime_ticks)
    df = pd.DataFrame(bars)
    if len(df) < 35:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["当日秒级/分时数据不足，无法计算秒级MACD"],
            "metrics": {"barCount": len(df), "source": "realtimeTicks"},
        }

    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    volume = numeric_series(df, "volume")
    amount = numeric_series(df, "amount")
    times = pd.to_numeric(df.get("time", pd.Series(dtype="float64")), errors="coerce")

    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    # 秒级MACD必须以独立实时 tick 为准，主图 quote 只在 tick 缺失时兜底。
    last_price = finite(close.iloc[-1]) or finite(quote.get("price"))
    last_time = finite(times.iloc[-1]) if not times.empty else None
    if last_price is None:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["最新价不可用，无法生成MACD交易信号"],
            "metrics": {"barCount": len(df), "source": "realtimeTicks"},
        }

    if last_time is not None:
        recent_mask = times >= last_time - 180_000
        recent_high_series = high[recent_mask].iloc[:-1]
        volume_mask = times >= last_time - 60_000
        volume_base = volume[volume_mask].iloc[:-1]
    else:
        recent_high_series = high.iloc[-61:-1]
        volume_base = volume.iloc[-21:-1]

    if recent_high_series.empty:
        recent_high_series = high.iloc[-min(len(high), 61):-1]
    recent_3min_high = finite(recent_high_series.max()) if not recent_high_series.empty else None

    if volume_base.empty:
        volume_base = volume.iloc[-21:-1]
    last_volume = finite(volume.iloc[-1]) or 0
    avg_volume = finite(volume_base.mean()) if not volume_base.empty else None
    volume_multiplier = last_volume / avg_volume if avg_volume else 1

    intraday_amount = finite(amount.sum()) or 0
    intraday_volume = finite(volume.sum()) or 0
    vwap = intraday_amount / intraday_volume if intraday_amount and intraday_volume else None
    short_ema = finite(close.ewm(span=10, adjust=False).mean().iloc[-1])

    dif_now = finite(dif.iloc[-1]) or 0
    dea_now = finite(dea.iloc[-1]) or 0
    hist_values = [finite(value) or 0 for value in hist.tail(4)]
    hist_positive = hist_values[-1] > 0
    hist_expanding_3 = len(hist_values) >= 3 and hist_values[-3] > 0 and hist_values[-2] > hist_values[-3] and hist_values[-1] > hist_values[-2]
    hist_shrinking_3 = len(hist_values) >= 3 and hist_values[-3] > 0 and hist_values[-2] < hist_values[-3] and hist_values[-1] < hist_values[-2]
    cross_up = len(dif) >= 2 and dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]
    cross_down = len(dif) >= 2 and dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]
    price_break_3min_high = recent_3min_high is not None and last_price > recent_3min_high
    volume_expand = volume_multiplier >= 1.5
    above_vwap = vwap is None or last_price >= vwap
    below_vwap = vwap is not None and last_price < vwap
    below_short_ema = short_ema is not None and last_price < short_ema

    strong_buy = dif_now > 0 and dea_now > 0 and hist_expanding_3 and price_break_3min_high and volume_expand
    buy = not strong_buy and (cross_up or (dif_now > dea_now and hist_positive)) and above_vwap and volume_multiplier >= 1.15
    sell = cross_down or hist_shrinking_3 or (below_vwap and below_short_ema)

    score = 45
    reasons = []
    risks = []
    if dif_now > 0 and dea_now > 0:
        score += 12
        reasons.append("DIF与DEA均在零轴上方")
    if hist_expanding_3:
        score += 20
        reasons.append("MACD红柱连续放大3根")
    if price_break_3min_high:
        score += 18
        reasons.append("股价突破最近3分钟高点")
    if volume_expand:
        score += 14
        reasons.append(f"成交量同步放大 {volume_multiplier:.2f}x")
    if above_vwap and vwap is not None:
        score += 6
        reasons.append("价格位于VWAP上方")
    if sell:
        score -= 28
        risks.append("MACD动能转弱或价格跌破短线均衡位")
    if below_vwap:
        risks.append("价格跌破VWAP，追击胜率下降")

    if strong_buy:
        label = "强买"
        action = "STRONG_BUY"
    elif buy:
        label = "买入"
        action = "BUY"
    elif sell:
        label = "卖出/止盈"
        action = "SELL"
    else:
        label = "观望"
        action = "WAIT"

    if not reasons:
        reasons.append("秒级MACD条件未形成共振")
    if action in {"STRONG_BUY", "BUY"} and not risks:
        risks.append("追击型信号需严格止损，红柱缩短或跌回突破位应撤退")

    return {
        "score": round(clamp(score)),
        "label": label,
        "action": action,
        "reasons": reasons[:5],
        "risks": risks[:4],
        "metrics": {
            "dif": dif_now,
            "dea": dea_now,
            "hist": hist_values[-1],
            "recent3MinHigh": recent_3min_high,
            "priceBreak3MinHigh": price_break_3min_high,
            "volumeMultiplier": volume_multiplier,
            "histExpanding3": hist_expanding_3,
            "histShrinking3": hist_shrinking_3,
            "vwap": vwap,
            "shortEma10": short_ema,
            "barCount": len(df),
            "barIntervalSeconds": 3,
            "source": "realtimeTicks",
        },
    }


def build_main_chart_macd_signal(
    candles: list[dict[str, Any]],
    range_name: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    """Calculate MACD from the currently displayed main-chart candles."""
    df = pd.DataFrame(candles)
    if len(df) < 35 or "close" not in df:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["主图K线数据不足，无法计算主图MACD"],
            "metrics": {"barCount": len(df), "source": "mainCandles", "range": range_name, "interval": interval},
        }

    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 35:
        return {
            "score": 0,
            "label": "不足",
            "action": "WAIT",
            "reasons": ["主图K线收盘价不足，无法计算主图MACD"],
            "metrics": {"barCount": len(close), "source": "mainCandles", "range": range_name, "interval": interval},
        }

    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = (dif - dea) * 2

    dif_now = finite(dif.iloc[-1]) or 0
    dea_now = finite(dea.iloc[-1]) or 0
    hist_now = finite(hist.iloc[-1]) or 0
    hist_prev = finite(hist.iloc[-2]) if len(hist) >= 2 else None
    hist_tail = [finite(value) or 0 for value in hist.tail(4)]
    cross_up = len(dif) >= 2 and dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]
    cross_down = len(dif) >= 2 and dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]
    hist_expanding_3 = len(hist_tail) >= 3 and hist_tail[-3] > 0 and hist_tail[-2] > hist_tail[-3] and hist_tail[-1] > hist_tail[-2]
    hist_shrinking_3 = len(hist_tail) >= 3 and hist_tail[-3] > 0 and hist_tail[-2] < hist_tail[-3] and hist_tail[-1] < hist_tail[-2]
    hist_turn_positive = hist_now > 0 and (hist_prev is None or hist_now >= hist_prev)
    hist_turn_negative = hist_now < 0 and (hist_prev is None or hist_now <= hist_prev)

    score = 50
    reasons = []
    risks = []
    if dif_now > 0 and dea_now > 0:
        score += 14
        reasons.append("DIF与DEA位于零轴上方")
    elif dif_now < 0 and dea_now < 0:
        score -= 14
        risks.append("DIF与DEA位于零轴下方")
    if cross_up:
        score += 16
        reasons.append("主图MACD金叉")
    if cross_down:
        score -= 16
        risks.append("主图MACD死叉")
    if hist_expanding_3:
        score += 14
        reasons.append("主图MACD红柱连续放大")
    elif hist_shrinking_3:
        score -= 12
        risks.append("主图MACD红柱连续缩短")
    elif hist_turn_positive:
        score += 8
        reasons.append("主图MACD柱体偏多")
    elif hist_turn_negative:
        score -= 8
        risks.append("主图MACD柱体偏空")

    if score >= 68:
        label = "偏多"
        action = "BUY_BIAS"
    elif score <= 36:
        label = "偏空"
        action = "SELL_BIAS"
    else:
        label = "震荡"
        action = "WAIT"

    if not reasons:
        reasons.append("主图MACD尚未形成明确多头信号")
    if label != "偏空" and not risks:
        risks.append("主图MACD仍需结合量能与盘口确认")

    return {
        "score": round(clamp(score)),
        "label": label,
        "action": action,
        "reasons": reasons[:5],
        "risks": risks[:4],
        "metrics": {
            "dif": dif_now,
            "dea": dea_now,
            "hist": hist_now,
            "histExpanding3": hist_expanding_3,
            "histShrinking3": hist_shrinking_3,
            "barCount": len(close),
            "source": "mainCandles",
            "range": range_name,
            "interval": interval,
        },
    }


def build_market_signals(
    quote: dict[str, Any],
    candles: list[dict[str, Any]],
    order_book: dict[str, Any],
    metrics: dict[str, Any],
    fund_flow: dict[str, Any] | None = None,
    realtime_ticks: list[dict[str, Any]] | None = None,
    range_name: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    main_chart_macd = build_main_chart_macd_signal(candles, range_name, interval)
    seconds_macd = build_seconds_macd_signal(realtime_ticks or [], quote)
    if df.empty:
        return {
            "mainAccumulation": {"score": 0, "label": "不足", "reasons": ["K线数据不足"]},
            "hotMoneyIgnition": {"score": 0, "label": "不足", "reasons": ["K线数据不足"]},
            "mainChartMacd": main_chart_macd,
            "secondsMacd": seconds_macd,
            "ddeFlow": {"score": 0, "label": "不足", "reasons": ["K线数据不足"], "metrics": {}},
            "bullTrap": {"score": 0, "label": "不足", "reasons": ["K线数据不足"]},
            "limitUpProbability": {"score": 0, "label": "低", "reasons": ["K线数据不足"]},
            "riskLevel": {"score": 60, "label": "中", "reasons": ["K线数据不足，风险默认偏中"]},
        }

    close = pd.to_numeric(df["close"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = numeric_series(df, "volume")
    amount = numeric_series(df, "amount")

    last = finite(quote.get("price")) or finite(close.iloc[-1])
    previous = finite(quote.get("previousClose"))
    open_price = finite(quote.get("open")) or finite(open_.iloc[0])
    day_high = finite(quote.get("dayHigh")) or finite(high.max())
    day_low = finite(quote.get("dayLow")) or finite(low.min())
    change_pct = finite(quote.get("changePercent"))
    if change_pct is None and last is not None and previous:
        change_pct = (last - previous) / previous * 100

    day_amplitude = (day_high - day_low) / previous * 100 if day_high and day_low and previous else 0
    close_position = (last - day_low) / max(day_high - day_low, 0.01) if last and day_high and day_low else 0.5
    intraday_return = (last - open_price) / open_price * 100 if last and open_price else 0
    recent_return_5 = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100 if len(close) > 6 and close.iloc[-6] else 0
    volume_ratio = finite(metrics.get("volumeRatio")) or 1
    rsi14 = finite(metrics.get("rsi14")) or 50
    order_imbalance = finite(order_book.get("imbalance")) or 0
    turnover_value = finite(amount.tail(20).sum()) or finite(quote.get("amount")) or 0
    avg_amount = finite(amount.tail(20).mean()) or 0
    amount_surge = amount.iloc[-1] / max(amount.tail(30).mean(), 1) if len(amount) >= 30 and amount.tail(30).mean() else 1
    upper_shadow = (day_high - last) / max(day_high - day_low, 0.01) if day_high and day_low and last else 0
    limit_pct = 20 if previous and previous < 1 else 10
    distance_to_limit = ((previous * (1 + limit_pct / 100)) - last) / previous * 100 if previous and last else 99

    accumulation_score = 35
    accumulation_reasons = []
    if volume_ratio >= 1.25:
        accumulation_score += 18
        accumulation_reasons.append(f"量能放大 {volume_ratio:.2f}x")
    if abs(change_pct or 0) <= 2.5 and day_amplitude <= 5.5:
        accumulation_score += 18
        accumulation_reasons.append("放量但价格波动受控")
    if close_position >= 0.45 and order_imbalance >= -0.15:
        accumulation_score += 14
        accumulation_reasons.append("收盘位置不弱且盘口未明显失衡")
    if avg_amount > 0:
        accumulation_score += 8
        accumulation_reasons.append("成交额连续性可用")
    if upper_shadow > 0.45:
        accumulation_score -= 18
        accumulation_reasons.append("上影线偏长，吸筹信号打折")

    ignition_score = 20
    ignition_reasons = []
    if change_pct is not None and change_pct >= 3:
        ignition_score += 25
        ignition_reasons.append(f"涨幅达到 {change_pct:.2f}%")
    if recent_return_5 >= 1.5:
        ignition_score += 18
        ignition_reasons.append(f"近5根快速拉升 {recent_return_5:.2f}%")
    if amount_surge >= 1.8 or volume_ratio >= 1.8:
        ignition_score += 20
        ignition_reasons.append("分时成交明显脉冲放大")
    if close_position >= 0.72:
        ignition_score += 12
        ignition_reasons.append("价格靠近日内高位")
    if order_imbalance > 0.2:
        ignition_score += 10
        ignition_reasons.append("买盘量差占优")

    dde_signal = build_dde_signal(fund_flow or estimate_dde_flow(quote, candles, order_book))
    dde_metrics = dde_signal.get("metrics") or {}
    dde_score = finite(dde_signal.get("score")) or 50
    dde_main_ratio = finite(dde_metrics.get("mainNetInflowRatio")) or 0
    if dde_score >= 65:
        accumulation_score += 10
        accumulation_reasons.append(f"DDE资金流{dde_signal['label']}")
    if dde_score >= 72 and dde_main_ratio > 0:
        ignition_score += 8
        ignition_reasons.append("DDE大单资金配合上攻")
    if dde_score <= 35 and dde_main_ratio < 0:
        accumulation_score -= 10
        accumulation_reasons.append("DDE显示主力净流出")

    bull_trap_score = 18
    bull_trap_reasons = []
    if upper_shadow >= 0.45 and change_pct and change_pct > 0:
        bull_trap_score += 26
        bull_trap_reasons.append("冲高回落，上影线偏长")
    if volume_ratio >= 1.6 and close_position <= 0.45:
        bull_trap_score += 24
        bull_trap_reasons.append("放量但收盘位置偏低")
    if rsi14 >= 72:
        bull_trap_score += 14
        bull_trap_reasons.append(f"RSI {rsi14:.1f} 偏热")
    if order_imbalance < -0.15:
        bull_trap_score += 12
        bull_trap_reasons.append("卖盘量差占优")
    if dde_score <= 35:
        bull_trap_score += 10
        bull_trap_reasons.append("DDE资金流偏流出")
    if change_pct is not None and change_pct < -1:
        bull_trap_score -= 8

    limit_score = 8
    limit_reasons = []
    if change_pct is not None:
        limit_score += clamp(change_pct * 6, -15, 45)
        limit_reasons.append(f"当前涨幅 {change_pct:.2f}%")
    if distance_to_limit <= 3:
        limit_score += 24
        limit_reasons.append(f"距涨停约 {distance_to_limit:.2f}%")
    if ignition_score >= 65:
        limit_score += 16
        limit_reasons.append("点火特征较强")
    if volume_ratio >= 1.8:
        limit_score += 12
        limit_reasons.append("量能支持上攻")
    if dde_score >= 70:
        limit_score += 10
        limit_reasons.append("DDE资金流入强化封板动能")
    if bull_trap_score >= 65:
        limit_score -= 22
        limit_reasons.append("诱多风险压低封板概率")
    if close_position < 0.6:
        limit_score -= 12

    risk_score = 30
    risk_reasons = []
    if bull_trap_score >= 60:
        risk_score += 25
        risk_reasons.append("诱多信号偏强")
    if day_amplitude >= 6:
        risk_score += 15
        risk_reasons.append(f"日内振幅 {day_amplitude:.2f}% 偏大")
    if rsi14 >= 75:
        risk_score += 14
        risk_reasons.append(f"RSI {rsi14:.1f} 过热")
    if order_imbalance < -0.25:
        risk_score += 12
        risk_reasons.append("卖盘压力较大")
    if dde_score <= 32:
        risk_score += 14
        risk_reasons.append("DDE显示资金流出压力")
    if accumulation_score >= 70 and bull_trap_score < 50:
        risk_score -= 10
        risk_reasons.append("吸筹特征缓和短线风险")
    if dde_score >= 68 and bull_trap_score < 55:
        risk_score -= 8
        risk_reasons.append("DDE资金承接改善风险")
    if turnover_value <= 0:
        risk_score += 8
        risk_reasons.append("成交额数据不足")

    risk_score = clamp(risk_score)
    risk_label = "高" if risk_score >= 70 else "中" if risk_score >= 45 else "低"

    return {
        "mainAccumulation": {
            "score": round(clamp(accumulation_score)),
            "label": label_by_score(accumulation_score, "较强", "观察", "较弱"),
            "reasons": accumulation_reasons[:4] or ["暂未出现明显吸筹特征"],
        },
        "hotMoneyIgnition": {
            "score": round(clamp(ignition_score)),
            "label": label_by_score(ignition_score, "明显", "观察", "不明显"),
            "reasons": ignition_reasons[:4] or ["暂未出现明显点火脉冲"],
        },
        "mainChartMacd": main_chart_macd,
        "secondsMacd": seconds_macd,
        "ddeFlow": dde_signal,
        "bullTrap": {
            "score": round(clamp(bull_trap_score)),
            "label": label_by_score(bull_trap_score, "高", "观察", "低"),
            "reasons": bull_trap_reasons[:4] or ["诱多特征暂不明显"],
        },
        "limitUpProbability": {
            "score": round(clamp(limit_score)),
            "label": label_by_score(limit_score, "较高", "中等", "较低"),
            "reasons": limit_reasons[:4] or ["距离涨停较远，封板动能不足"],
        },
        "riskLevel": {
            "score": round(risk_score),
            "label": risk_label,
            "reasons": risk_reasons[:4] or ["风险处于常规区间"],
        },
        "raw": {
            "changePercent": change_pct,
            "dayAmplitude": day_amplitude,
            "closePosition": close_position,
            "recentReturn5": recent_return_5,
            "orderImbalance": order_imbalance,
        },
    }


def detect_kline_patterns(candles: list[dict[str, Any]], metrics: dict[str, Any]) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    if len(df) < 5:
        return {
            "trend": {"label": "不足", "score": 0, "reasons": ["K线数量不足"]},
            "patterns": [],
            "supportResistance": {
                "support": metrics.get("support"),
                "resistance": metrics.get("resistance"),
                "position": "未知",
            },
            "summary": "K线数据不足，无法识别形态。",
        }

    close = pd.to_numeric(df["close"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = numeric_series(df, "volume")

    recent = df.tail(30).copy()
    last_open = finite(open_.iloc[-1]) or 0
    last_close = finite(close.iloc[-1]) or 0
    last_high = finite(high.iloc[-1]) or 0
    last_low = finite(low.iloc[-1]) or 0
    prev_open = finite(open_.iloc[-2]) or 0
    prev_close = finite(close.iloc[-2]) or 0
    body = abs(last_close - last_open)
    candle_range = max(last_high - last_low, 0.01)
    upper_shadow = (last_high - max(last_open, last_close)) / candle_range
    lower_shadow = (min(last_open, last_close) - last_low) / candle_range
    body_ratio = body / candle_range
    volume_ratio = (volume.iloc[-1] / max(volume.tail(30).mean(), 1)) if len(volume) >= 30 else 1
    ma5 = close.tail(5).mean()
    ma10 = close.tail(10).mean() if len(close) >= 10 else ma5
    ma20 = close.tail(20).mean() if len(close) >= 20 else ma10
    slope_5 = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100 if len(close) > 6 and close.iloc[-6] else 0
    slope_20 = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100 if len(close) > 21 and close.iloc[-21] else slope_5
    resistance = finite(high.tail(30).max())
    support = finite(low.tail(30).min())
    position = "中位"
    if support is not None and resistance is not None and last_close:
        pos_value = (last_close - support) / max(resistance - support, 0.01)
        if pos_value >= 0.75:
            position = "接近压力"
        elif pos_value <= 0.25:
            position = "接近支撑"

    trend_score = 50
    trend_reasons = []
    if ma5 > ma10 > ma20:
        trend_score += 22
        trend_reasons.append("短中期均线多头排列")
    elif ma5 < ma10 < ma20:
        trend_score -= 22
        trend_reasons.append("短中期均线空头排列")
    if slope_5 > 1:
        trend_score += 12
        trend_reasons.append(f"近5根上涨 {slope_5:.2f}%")
    elif slope_5 < -1:
        trend_score -= 12
        trend_reasons.append(f"近5根下跌 {abs(slope_5):.2f}%")
    if slope_20 > 3:
        trend_score += 10
        trend_reasons.append("20周期趋势向上")
    elif slope_20 < -3:
        trend_score -= 10
        trend_reasons.append("20周期趋势向下")

    trend_score = clamp(trend_score)
    trend_label = "上升趋势" if trend_score >= 65 else "下降趋势" if trend_score <= 35 else "震荡趋势"

    patterns = []

    def add_pattern(name: str, direction: str, strength: float, reason: str) -> None:
        patterns.append(
            {
                "name": name,
                "direction": direction,
                "strength": round(clamp(strength)),
                "reason": reason,
            }
        )

    if body_ratio <= 0.18:
        add_pattern("十字星", "中性", 45 + min(volume_ratio * 8, 20), "实体很小，短线多空分歧加大")
    if lower_shadow >= 0.55 and body_ratio <= 0.35:
        add_pattern("锤头线", "看多", 58 + min(volume_ratio * 10, 25), "下影线较长，低位承接增强")
    if upper_shadow >= 0.55 and body_ratio <= 0.35:
        add_pattern("射击之星", "看空", 58 + min(volume_ratio * 10, 25), "上影线较长，冲高抛压明显")
    if last_close > last_open and prev_close < prev_open and last_close >= prev_open and last_open <= prev_close:
        add_pattern("阳包阴", "看多", 72, "最新阳线反包前一根阴线")
    if last_close < last_open and prev_close > prev_open and last_open >= prev_close and last_close <= prev_open:
        add_pattern("阴包阳", "看空", 72, "最新阴线吞没前一根阳线")
    if len(close) >= 3:
        c1, c2, c3 = close.iloc[-3], close.iloc[-2], close.iloc[-1]
        o1, o2, o3 = open_.iloc[-3], open_.iloc[-2], open_.iloc[-1]
        if c1 < o1 and abs(c2 - o2) / max(high.iloc[-2] - low.iloc[-2], 0.01) < 0.3 and c3 > o3 and c3 > (o1 + c1) / 2:
            add_pattern("早晨之星", "看多", 78, "三根K线出现弱转强结构")
        if c1 > o1 and abs(c2 - o2) / max(high.iloc[-2] - low.iloc[-2], 0.01) < 0.3 and c3 < o3 and c3 < (o1 + c1) / 2:
            add_pattern("黄昏之星", "看空", 78, "三根K线出现强转弱结构")
    if resistance and last_close > resistance * 0.995 and volume_ratio >= 1.4:
        add_pattern("放量突破", "看多", 76, "价格接近或突破近期压力且量能放大")
    if support and last_close < support * 1.005 and volume_ratio >= 1.4:
        add_pattern("放量破位", "看空", 76, "价格接近或跌破近期支撑且量能放大")

    patterns = sorted(patterns, key=lambda item: item["strength"], reverse=True)[:5]
    if not patterns:
        add_pattern("无明显形态", "中性", 30, "最近K线未形成高置信度经典形态")

    summary = f"{trend_label}，当前位置{position}。"
    if patterns:
        top = patterns[0]
        summary += f" 主要形态：{top['name']}（{top['direction']}，强度{top['strength']}）。"

    return {
        "trend": {"label": trend_label, "score": round(trend_score), "reasons": trend_reasons[:4] or ["均线和斜率信号不明显"]},
        "patterns": patterns,
        "supportResistance": {"support": support, "resistance": resistance, "position": position},
        "summary": summary,
    }


def heuristic_analysis(
    symbol: str,
    quote: dict[str, Any],
    candles: list[dict[str, Any]],
    order_book: dict[str, Any],
    fund_flow: dict[str, Any] | None = None,
    realtime_ticks: list[dict[str, Any]] | None = None,
    range_name: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    metrics = compute_indicators(candles)
    signals = build_market_signals(quote, candles, order_book, metrics, fund_flow, realtime_ticks, range_name, interval)
    kline = detect_kline_patterns(candles, metrics)
    last = quote.get("price") or (candles[-1]["close"] if candles else None)
    score = 0.0
    reasons = []
    risks = []

    if last and metrics["ma20"]:
        above = (last - metrics["ma20"]) / metrics["ma20"]
        score += 1 if above > 0 else -1
        reasons.append(f"价格{'站上' if above > 0 else '跌破'}20周期均线 {abs(above * 100):.2f}%")
    if metrics["ma20"] and metrics["ma60"]:
        score += 1 if metrics["ma20"] > metrics["ma60"] else -1
        reasons.append(f"20周期均线{'高于' if metrics['ma20'] > metrics['ma60'] else '低于'}60周期均线")
    if metrics["rsi14"] is not None:
        if metrics["rsi14"] > 70:
            score -= 0.5
            risks.append("RSI处于偏热区间，追高回撤风险上升")
        elif metrics["rsi14"] < 30:
            score += 0.5
            risks.append("RSI处于偏冷区间，反弹和弱势延续都需要观察")
        else:
            reasons.append(f"RSI {metrics['rsi14']:.1f}，动能未进入极端区")
    if metrics["volumeRatio"] and metrics["volumeRatio"] > 1.4:
        score += 0.8 if (quote.get("changePercent") or 0) >= 0 else -0.8
        reasons.append(f"近5根成交量约为基准的 {metrics['volumeRatio']:.2f} 倍")
    if order_book.get("imbalance") is not None:
        score += order_book["imbalance"] * 1.5
        reasons.append(f"盘口买卖量差 {order_book['imbalance'] * 100:.1f}%")
    dde_flow = signals.get("ddeFlow") or {}
    dde_score = finite(dde_flow.get("score")) or 50
    if dde_score >= 68:
        score += 0.8
        reasons.append(f"DDE资金流{dde_flow.get('label', '流入')}")
    elif dde_score <= 32:
        score -= 0.8
        risks.append(f"DDE资金流{dde_flow.get('label', '流出')}")

    direction = "偏多" if score > 1.1 else "偏空" if score < -1.1 else "震荡"
    main_chart_macd = signals.get("mainChartMacd") or {}
    if main_chart_macd.get("action") == "BUY_BIAS":
        score += 0.9
        reasons.append("主图MACD偏多，当前主图周期动能改善")
    elif main_chart_macd.get("action") == "SELL_BIAS":
        score -= 0.9
        risks.append("主图MACD偏空，当前主图周期动能转弱")

    seconds_macd = signals.get("secondsMacd") or {}
    if seconds_macd.get("action") == "STRONG_BUY":
        score += 2
        reasons.append("秒级MACD强买：零轴上方红柱放大、突破3分钟高点并放量")
    elif seconds_macd.get("action") == "BUY":
        score += 1
        reasons.append("秒级MACD偏多，短线动能改善")
    elif seconds_macd.get("action") == "SELL":
        score -= 2
        risks.append("秒级MACD卖出/止盈，短线动能转弱")

    direction = "偏多" if score > 1.1 else "偏空" if score < -1.1 else "震荡"
    confidence = max(35, min(88, 48 + abs(score) * 13 + min(metrics.get("volumeRatio") or 1, 2) * 4))
    action = {
        "偏多": "关注回踩均线后的承接，避免直接追涨。",
        "偏空": "关注反抽压力和止损纪律，弱势下不急于抄底。",
        "震荡": "等待放量突破或跌破区间后再提高仓位。",
    }[direction]
    if metrics["support"] and metrics["resistance"]:
        risks.append(f"近20周期压力约 {metrics['resistance']:.2f}，支撑约 {metrics['support']:.2f}")
    risks.extend(signals["riskLevel"]["reasons"][:2])
    risks.extend((signals.get("mainChartMacd") or {}).get("risks", [])[:1])
    risks.extend((signals.get("secondsMacd") or {}).get("risks", [])[:2])

    signal_summary = (
        f"主力吸筹{signals['mainAccumulation']['label']}，"
        f"游资点火{signals['hotMoneyIgnition']['label']}，"
        f"DDE资金流{signals['ddeFlow']['label']}，"
        f"诱多风险{signals['bullTrap']['label']}，"
        f"封板概率{signals['limitUpProbability']['score']}%，"
        f"风险等级{signals['riskLevel']['label']}。"
        f"K线：{kline['summary']}"
    )

    return {
        "engine": "heuristic",
        "symbol": symbol,
        "direction": direction,
        "confidence": round(confidence),
        "score": round(score, 2),
        "summary": f"{symbol} 当前判断为{direction}，置信度 {round(confidence)}%。{signal_summary}{action}",
        "metrics": metrics,
        "signals": signals,
        "kline": kline,
        "reasons": reasons[:5],
        "risks": risks[:4],
        "disclaimer": "仅用于行情研究和策略辅助，不构成投资建议。",
    }


async def ai_analysis(payload: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    provider = os.getenv("AI_PROVIDER", "openai").lower()
    if provider == "gemini":
        return await gemini_analysis(payload, heuristic)
    return await openai_analysis(payload, heuristic)


def third_party_unavailable(provider: str, status: str, reason: str, request_id: str | None = None) -> dict[str, Any]:
    result = {
        "engine": provider,
        "aiProvider": provider,
        "aiStatus": status,
        "aiReason": reason,
        "available": False,
        "direction": None,
        "confidence": None,
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


def compact_payload(payload: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": payload.get("symbol"),
        "quote": payload.get("quote"),
        "orderBook": {
            "imbalance": (payload.get("orderBook") or {}).get("imbalance"),
            "provider": (payload.get("orderBook") or {}).get("provider"),
        },
        "fundFlow": payload.get("fundFlow"),
        "recentCandles": (payload.get("candles") or [])[-80:],
        "heuristic": heuristic,
    }


async def openai_analysis(payload: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return third_party_unavailable("openai", "disabled", "OPENAI_API_KEY is not visible to FastAPI.")
    if os.getenv("USE_OPENAI", "1") != "1":
        return third_party_unavailable("openai", "disabled", "USE_OPENAI must be 1.")

    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "input": [
            {"role": "system", "content": "你是股票行情研究助手。基于报价、盘口和K线指标给出短线行情解读。必须强调不构成投资建议。输出JSON。"},
            {"role": "user", "content": f"请分析以下行情数据，返回字段：direction, confidence, summary, reasons, risks, metrics, signals, kline。signals 必须包含 mainAccumulation, hotMoneyIgnition, mainChartMacd, secondsMacd, ddeFlow, bullTrap, limitUpProbability, riskLevel，每项包含 score, label, reasons。mainChartMacd 基于主图 candles，secondsMacd 基于独立当日秒级/分时数据。kline 必须包含 trend, patterns, supportResistance, summary。\n{json.dumps(compact_payload(payload, heuristic), ensure_ascii=False)}"},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json=body,
            )
        request_id = response.headers.get("x-request-id")
        if response.status_code >= 400:
            error = response.json().get("error", {}).get("message", response.text)
            return third_party_unavailable("openai", "failed", error, request_id)
        data = response.json()
        text = data.get("output_text")
        if not text:
            for item in data.get("output", []):
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text = part.get("text")
                        break
        parsed = json.loads(text)
        return {**parsed, "engine": "openai", "aiStatus": "ok", "aiProvider": "openai", "aiRequestId": request_id, "available": True}
    except Exception as exc:
        return third_party_unavailable("openai", "failed", str(exc))


async def gemini_analysis(payload: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return third_party_unavailable("gemini", "disabled", "GEMINI_API_KEY is not visible to FastAPI.")

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = (
        "你是股票行情研究助手。基于报价、盘口和K线指标给出短线行情解读。"
        "返回严格JSON，字段：direction, confidence, summary, reasons, risks, metrics, signals, kline。"
        "signals 必须包含 mainAccumulation, hotMoneyIgnition, mainChartMacd, secondsMacd, ddeFlow, bullTrap, limitUpProbability, riskLevel，每项包含 score, label, reasons。"
        "mainChartMacd 基于主图 candles，secondsMacd 基于独立当日秒级/分时数据。"
        "kline 必须包含 trend, patterns, supportResistance, summary。"
        "必须强调不构成投资建议。\n"
        f"{json.dumps(compact_payload(payload, heuristic), ensure_ascii=False)}"
    )
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
            return third_party_unavailable("gemini", "failed", error)
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        return {**parsed, "engine": "gemini", "aiStatus": "ok", "aiProvider": "gemini", "available": True}
    except Exception as exc:
        return third_party_unavailable("gemini", "failed", str(exc))


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
            await asyncio.to_thread(
                save_market_candles,
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
        await asyncio.to_thread(
            save_market_candles,
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
        try:
            data = await ifind_realtime_quote(a_share, ttl=1)
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
            data = await sina_quote(a_share)
            quote = data.get("quote") or {}
            price = finite(quote.get("price"))
            if price is None:
                raise HTTPException(status_code=502, detail=f"realtime quote unavailable: {ifind_exc}")
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

    normalized_symbol = symbol.strip().upper()
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
        raise HTTPException(status_code=502, detail=f"realtime quote unavailable: {exc}") from exc


@app.post("/api/analyze")
async def analyze(request: Request):
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except UnicodeDecodeError:
        payload = json.loads(raw_body.decode("gb18030", errors="replace"))
    heuristic = heuristic_analysis(
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


@app.exception_handler(Exception)
async def all_exception_handler(_: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
