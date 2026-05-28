import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "l2llm.db"
DUCKDB_FILE = DATA_DIR / "l2llm.duckdb"


class Base(DeclarativeBase):
    pass


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("market", "symbol", "interval", "time", name="uq_market_candle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    interval: Mapped[str] = mapped_column(String(16), index=True)
    time: Mapped[int] = mapped_column(Integer, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AiAnalysisHistory(Base):
    __tablename__ = "ai_analysis_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    range_name: Mapped[str] = mapped_column(String(16), index=True)
    interval: Mapped[str] = mapped_column(String(16), index=True)
    local_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    third_party_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_FILE}", future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_sqlite_columns()
    repair_future_china_candles()
    init_duckdb()


def ensure_sqlite_columns() -> None:
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(market_candles)")).fetchall()}
        if "amount" not in columns:
            conn.execute(text("ALTER TABLE market_candles ADD COLUMN amount FLOAT"))


def duckdb_enabled() -> bool:
    return os.getenv("USE_DUCKDB", "1") != "0"


_duckdb_error: str | None = None


def duckdb_connect():
    global _duckdb_error
    if not duckdb_enabled():
        return None
    try:
        import duckdb

        connection = duckdb.connect(str(DUCKDB_FILE))
        _duckdb_error = None
        return connection
    except Exception as exc:
        _duckdb_error = str(exc)
        return None


def init_duckdb() -> None:
    connection = duckdb_connect()
    if connection is None:
        return
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_candles (
              market VARCHAR,
              symbol VARCHAR,
              interval VARCHAR,
              time BIGINT,
              open DOUBLE,
              high DOUBLE,
              low DOUBLE,
              close DOUBLE,
              volume DOUBLE,
              amount DOUBLE,
              provider VARCHAR,
              created_at TIMESTAMP,
              PRIMARY KEY (market, symbol, interval, time)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_analysis_history (
              id UBIGINT,
              market VARCHAR,
              symbol VARCHAR,
              range_name VARCHAR,
              interval VARCHAR,
              local_json VARCHAR,
              third_party_json VARCHAR,
              created_at TIMESTAMP
            )
            """
        )
    except Exception as exc:
        global _duckdb_error
        _duckdb_error = str(exc)
    finally:
        connection.close()


def repair_future_china_candles() -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    offset_ms = 8 * 60 * 60 * 1000
    day_ms = 24 * 60 * 60 * 1000
    future_cutoff = now_ms + 10 * 60 * 1000
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM market_candles
                WHERE market = 'cn'
                  AND time > :future_cutoff
                  AND EXISTS (
                    SELECT 1 FROM market_candles AS corrected
                    WHERE corrected.market = market_candles.market
                      AND corrected.symbol = market_candles.symbol
                      AND corrected.interval = market_candles.interval
                      AND corrected.time = market_candles.time - :offset_ms
                  )
                """
            ),
            {"future_cutoff": future_cutoff, "offset_ms": offset_ms},
        )
        conn.execute(
            text(
                """
                UPDATE market_candles
                SET time = time - :offset_ms
                WHERE market = 'cn'
                  AND time > :future_cutoff
                """
            ),
            {"future_cutoff": future_cutoff, "offset_ms": offset_ms},
        )
        conn.execute(
            text(
                """
                DELETE FROM market_candles
                WHERE market = 'cn'
                  AND interval = '1d'
                  AND (time % :day_ms) = 0
                  AND EXISTS (
                    SELECT 1 FROM market_candles AS corrected
                    WHERE corrected.market = market_candles.market
                      AND corrected.symbol = market_candles.symbol
                      AND corrected.interval = market_candles.interval
                      AND corrected.time = market_candles.time - :offset_ms
                  )
                """
            ),
            {"day_ms": day_ms, "offset_ms": offset_ms},
        )
        conn.execute(
            text(
                """
                UPDATE market_candles
                SET time = time - :offset_ms
                WHERE market = 'cn'
                  AND interval = '1d'
                  AND (time % :day_ms) = 0
                """
            ),
            {"day_ms": day_ms, "offset_ms": offset_ms},
        )


def normalize_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def save_market_candles(
    *,
    market: str,
    symbol: str,
    interval: str,
    provider: str,
    candles: list[dict[str, Any]],
) -> int:
    if not candles:
        return 0

    rows = []
    for candle in candles:
        timestamp = candle.get("time")
        if timestamp is None:
            continue
        rows.append(
            {
                "market": market,
                "symbol": symbol,
                "interval": interval,
                "time": int(timestamp),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "volume": candle.get("volume"),
                "amount": candle.get("amount"),
                "provider": provider,
            }
        )
    # Some providers can return duplicate bucket timestamps after long-range
    # resampling. Deduplicate before inserting so SQLite's unique key is not
    # tripped by two pending rows from the same request.
    rows = list({(row["market"], row["symbol"], row["interval"], row["time"]): row for row in rows}.values())
    if not rows:
        return 0

    duckdb_saved = save_market_candles_duckdb(rows)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        values = [{**row, "created_at": now} for row in rows]
        statement = sqlite_insert(MarketCandle).values(values)
        excluded = statement.excluded
        # SQLite handles the unique-key race atomically here. This avoids the
        # old select-then-insert window when several refreshes save the same bar.
        statement = statement.on_conflict_do_update(
            index_elements=["market", "symbol", "interval", "time"],
            set_={
                "open": excluded.open,
                "high": excluded.high,
                "low": excluded.low,
                "close": excluded.close,
                "volume": excluded.volume,
                "amount": excluded.amount,
                "provider": excluded.provider,
            },
        )
        session.execute(statement)
        session.commit()
    return max(duckdb_saved, len(rows))


def save_market_candles_duckdb(rows: list[dict[str, Any]]) -> int:
    connection = duckdb_connect()
    if connection is None or not rows:
        return 0
    now = datetime.now(timezone.utc)
    try:
        # DuckDB is the local analytical history store. Delete-then-insert gives
        # deterministic upserts on the market/symbol/interval/time key.
        connection.execute("BEGIN TRANSACTION")
        connection.executemany(
            """
            DELETE FROM market_candles
            WHERE market = ? AND symbol = ? AND interval = ? AND time = ?
            """,
            [(row["market"], row["symbol"], row["interval"], row["time"]) for row in rows],
        )
        connection.executemany(
            """
            INSERT INTO market_candles
              (market, symbol, interval, time, open, high, low, close, volume, amount, provider, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["market"],
                    row["symbol"],
                    row["interval"],
                    row["time"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row.get("amount"),
                    row["provider"],
                    now,
                )
                for row in rows
            ],
        )
        connection.execute("COMMIT")
        return len(rows)
    except Exception as exc:
        global _duckdb_error
        _duckdb_error = str(exc)
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        return 0
    finally:
        connection.close()


def list_market_candles(
    *,
    market: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 5000))
    duckdb_rows = list_market_candles_duckdb(market=market, symbol=symbol, interval=interval, limit=limit)
    if duckdb_rows:
        return duckdb_rows
    with SessionLocal() as session:
        statement = select(MarketCandle)
        if market:
            statement = statement.where(MarketCandle.market == market)
        if symbol:
            statement = statement.where(MarketCandle.symbol == symbol)
        if interval:
            statement = statement.where(MarketCandle.interval == interval)
        rows = session.execute(statement.order_by(MarketCandle.time.desc()).limit(limit)).scalars().all()
    return [
        {
            "market": row.market,
            "symbol": row.symbol,
            "interval": row.interval,
            "time": row.time,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
            "provider": row.provider,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }
        for row in reversed(rows)
    ]


def list_market_candles_duckdb(
    *,
    market: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    connection = duckdb_connect()
    if connection is None:
        return []
    conditions = []
    params: list[Any] = []
    if market:
        conditions.append("market = ?")
        params.append(market)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if interval:
        conditions.append("interval = ?")
        params.append(interval)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT market, symbol, interval, time, open, high, low, close, volume, amount, provider, created_at
        FROM market_candles
        {where}
        ORDER BY time DESC
        LIMIT ?
    """
    try:
        rows = connection.execute(query, [*params, limit]).fetchall()
        rows = [
            {
                "market": row[0],
                "symbol": row[1],
                "interval": row[2],
                "time": row[3],
                "open": row[4],
                "high": row[5],
                "low": row[6],
                "close": row[7],
                "volume": row[8],
                "amount": row[9],
                "provider": row[10],
                "createdAt": row[11].isoformat() if row[11] else None,
            }
            for row in rows
        ]
        return list(reversed(rows))
    except Exception as exc:
        global _duckdb_error
        _duckdb_error = str(exc)
        return []
    finally:
        connection.close()


def save_ai_analysis(
    *,
    market: str,
    symbol: str,
    range_name: str,
    interval: str,
    local: dict[str, Any],
    third_party: dict[str, Any],
) -> int:
    duckdb_id = save_ai_analysis_duckdb(
        market=market,
        symbol=symbol,
        range_name=range_name,
        interval=interval,
        local=local,
        third_party=third_party,
    )
    with SessionLocal() as session:
        row = AiAnalysisHistory(
            market=market,
            symbol=symbol,
            range_name=range_name,
            interval=interval,
            local_json=normalize_json(local),
            third_party_json=normalize_json(third_party),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return duckdb_id or row.id


def save_ai_analysis_duckdb(
    *,
    market: str,
    symbol: str,
    range_name: str,
    interval: str,
    local: dict[str, Any],
    third_party: dict[str, Any],
) -> int:
    connection = duckdb_connect()
    if connection is None:
        return 0
    row_id = time.time_ns()
    now = datetime.now(timezone.utc)
    try:
        connection.execute(
            """
            INSERT INTO ai_analysis_history
              (id, market, symbol, range_name, interval, local_json, third_party_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                market,
                symbol,
                range_name,
                interval,
                json.dumps(normalize_json(local), ensure_ascii=False),
                json.dumps(normalize_json(third_party), ensure_ascii=False),
                now,
            ),
        )
        return row_id
    except Exception as exc:
        global _duckdb_error
        _duckdb_error = str(exc)
        return 0
    finally:
        connection.close()


def list_ai_analysis(
    *,
    market: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 1000))
    duckdb_rows = list_ai_analysis_duckdb(market=market, symbol=symbol, limit=limit)
    if duckdb_rows:
        return duckdb_rows
    with SessionLocal() as session:
        statement = select(AiAnalysisHistory)
        if market:
            statement = statement.where(AiAnalysisHistory.market == market)
        if symbol:
            statement = statement.where(AiAnalysisHistory.symbol == symbol)
        rows = session.execute(statement.order_by(AiAnalysisHistory.created_at.desc()).limit(limit)).scalars().all()
    return [
        {
            "id": row.id,
            "market": row.market,
            "symbol": row.symbol,
            "range": row.range_name,
            "interval": row.interval,
            "local": row.local_json,
            "thirdParty": row.third_party_json,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def list_ai_analysis_duckdb(
    *,
    market: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    connection = duckdb_connect()
    if connection is None:
        return []
    conditions = []
    params: list[Any] = []
    if market:
        conditions.append("market = ?")
        params.append(market)
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT id, market, symbol, range_name, interval, local_json, third_party_json, created_at
        FROM ai_analysis_history
        {where}
        ORDER BY created_at DESC
        LIMIT ?
    """
    try:
        rows = connection.execute(query, [*params, limit]).fetchall()
        return [
            {
                "id": row[0],
                "market": row[1],
                "symbol": row[2],
                "range": row[3],
                "interval": row[4],
                "local": json.loads(row[5]) if row[5] else {},
                "thirdParty": json.loads(row[6]) if row[6] else {},
                "createdAt": row[7].isoformat() if row[7] else None,
            }
            for row in rows
        ]
    except Exception as exc:
        global _duckdb_error
        _duckdb_error = str(exc)
        return []
    finally:
        connection.close()


def storage_status() -> dict[str, Any]:
    connection = duckdb_connect()
    if connection is not None:
        connection.close()
    return {
        "sqlite": {"enabled": True, "path": str(DB_FILE)},
        "duckdb": {
            "enabled": duckdb_enabled(),
            "available": connection is not None,
            "path": str(DUCKDB_FILE),
            "error": _duckdb_error,
        },
    }
