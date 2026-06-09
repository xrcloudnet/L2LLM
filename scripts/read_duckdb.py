import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import duckdb
except ImportError as exc:
    raise SystemExit(
        "duckdb is not installed in the selected Python environment. "
        "Use the project runtime Python or run: pip install duckdb"
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "l2llm.duckdb"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def rows_to_dicts(columns: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, indent=2))


def query_table(connection: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    result = connection.execute(sql, params or [])
    columns = [column[0] for column in result.description]
    return rows_to_dicts(columns, result.fetchall())


def list_tables(connection: Any) -> None:
    print_json(query_table(connection, "SHOW TABLES"))


def show_counts(connection: Any) -> None:
    output = {}
    for table in ["market_candles", "ai_analysis_history"]:
        try:
            output[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception as exc:
            output[table] = f"error: {exc}"
    print_json(output)


def show_candles(connection: Any, args: argparse.Namespace) -> None:
    conditions = []
    params: list[Any] = []
    if args.market:
        conditions.append("market = ?")
        params.append(args.market)
    if args.symbol:
        conditions.append("symbol = ?")
        params.append(args.symbol)
    if args.interval:
        conditions.append("interval = ?")
        params.append(args.interval)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT market, symbol, interval, time, open, high, low, close, volume, amount, provider, created_at
        FROM market_candles
        {where}
        ORDER BY time DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = query_table(connection, sql, params)
    print_json(list(reversed(rows)))


def show_analysis(connection: Any, args: argparse.Namespace) -> None:
    conditions = []
    params: list[Any] = []
    if args.market:
        conditions.append("market = ?")
        params.append(args.market)
    if args.symbol:
        conditions.append("symbol = ?")
        params.append(args.symbol)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT id, market, symbol, range_name, interval, local_json, third_party_json, created_at
        FROM ai_analysis_history
        {where}
        ORDER BY created_at DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = query_table(connection, sql, params)
    for row in rows:
        for key in ["local_json", "third_party_json"]:
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except json.JSONDecodeError:
                    pass
    print_json(rows)


def run_sql(connection: Any, sql: str) -> None:
    print_json(query_table(connection, sql))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read L2LLM DuckDB data.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="DuckDB file path.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tables", help="List DuckDB tables.")
    sub.add_parser("counts", help="Show row counts.")

    candles = sub.add_parser("candles", help="Show recent market candles.")
    candles.add_argument("--market", choices=["cn", "us", "hk"])
    candles.add_argument("--symbol")
    candles.add_argument("--interval")
    candles.add_argument("--limit", type=int, default=20)

    analysis = sub.add_parser("analysis", help="Show recent AI analysis history.")
    analysis.add_argument("--market", choices=["cn", "us", "hk"])
    analysis.add_argument("--symbol")
    analysis.add_argument("--limit", type=int, default=10)

    sql = sub.add_parser("sql", help="Run a read-only SQL query.")
    sql.add_argument("query")

    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DuckDB file not found: {db_path}")

    with duckdb.connect(str(db_path), read_only=True) as connection:
        if args.command == "tables":
            list_tables(connection)
        elif args.command == "counts":
            show_counts(connection)
        elif args.command == "candles":
            show_candles(connection, args)
        elif args.command == "analysis":
            show_analysis(connection, args)
        elif args.command == "sql":
            run_sql(connection, args.query)


if __name__ == "__main__":
    main()
