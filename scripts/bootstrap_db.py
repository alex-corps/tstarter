"""Готовит базу к работе генератора.

На пустой базе создаёт схему (sql/000_schema.sql) и загружает справочники из data/seed.
На старой базе, где sale_order_day ещё без date_dt, применяет sql/001_alter.sql.
Если заказы ещё лежат в income_order_day, применяет sql/002_order_headers.sql.
Повторный запуск ничего не перезаписывает.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gen.db import connect  # noqa: E402

SEED = ROOT / "data" / "seed"
SCHEMA = ROOT / "sql" / "000_schema.sql"
ALTER = ROOT / "sql" / "001_alter.sql"
HEADERS = ROOT / "sql" / "002_order_headers.sql"

# порядок важен: внешние ключи
SEED_TABLES = [
    "dim_category",
    "dim_supplier",
    "dim_customer",
    "dim_products",
    "dim_product_crosses",
    "supplier_schedule",
    "current_assortment",
]


def _apply(conn, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "select 1 from information_schema.tables where table_schema = 'public' and table_name = %s",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str, schema: str = "public") -> bool:
    row = conn.execute(
        """
        select 1 from information_schema.columns
        where table_schema = %s and table_name = %s and column_name = %s
        """,
        (schema, table, column),
    ).fetchone()
    return row is not None


def _needs_order_headers(conn) -> bool:
    return (
        not _table_exists(conn, "income_order")
        or not _column_exists(conn, "supplier_schedule", "income_order_dt")
        or not _column_exists(conn, "dim_supplier", "order_day_of_week")
        or not _column_exists(conn, "backorder", "income_order_id", schema="gen_internal")
    )


def _load_seed(conn) -> None:
    filled = conn.execute("select count(*) from dim_products").fetchone()[0]
    if filled:
        print(f"справочники уже загружены ({filled} SKU), загрузка пропущена")
        return
    for table in SEED_TABLES:
        path = SEED / f"{table}.csv"
        sql = f"copy {table} from stdin with (format csv, header true)"
        with conn.cursor() as cur, cur.copy(sql) as copy:
            copy.write(path.read_bytes())
        print(f"загружен {table}")
    conn.commit()


def main() -> None:
    conn = connect()
    if not _table_exists(conn, "dim_products"):
        print("создаю схему")
        _apply(conn, SCHEMA)
    else:
        if not _column_exists(conn, "sale_order_day", "date_dt"):
            print("применяю миграцию 001_alter.sql")
            _apply(conn, ALTER)
        if _needs_order_headers(conn):
            print("применяю миграцию 002_order_headers.sql")
            _apply(conn, HEADERS)
        else:
            print("схема актуальна")
    _load_seed(conn)
    conn.close()


if __name__ == "__main__":
    main()
