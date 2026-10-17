"""Подключение, массовая запись и откат рассчитанных данных."""

import psycopg

from gen.config import DEFAULT_PARAMS, DSN

FACT_TABLES = ("sale_order_day", "income_day", "income_order_day", "rest_day")


def connect():
    return psycopg.connect(DSN)


def copy_rows(conn, table: str, columns: list[str], rows) -> int:
    """Массовая вставка. rows — итератор кортежей."""
    if not rows:
        return 0
    sql = f"copy {table} ({', '.join(columns)}) from stdin"
    with conn.cursor() as cur, cur.copy(sql) as cp:
        for row in rows:
            cp.write_row(row)
    return len(rows)


def rollback_from(conn, date_from) -> None:
    """Удаляет все рассчитанные данные начиная с даты, чтобы период можно было пересчитать."""
    with conn.cursor() as cur:
        for table in FACT_TABLES:
            cur.execute(f"delete from {table} where date_dt >= %s", (date_from,))
        cur.execute("delete from gen_internal.truth_demand where date_dt >= %s", (date_from,))
        cur.execute("delete from gen_internal.calc_log where date_dt >= %s", (date_from,))
        cur.execute("delete from gen_internal.backorder where order_dt >= %s", (date_from,))
        cur.execute(
            "delete from current_assortment where date_from_dt >= %s and date_from_dt > date '2024-01-01'",
            (date_from,),
        )
        cur.execute(
            "update current_assortment set date_to_dt = null, status = 'активный' "
            "where date_to_dt >= %s and status = 'вывод'",
            (date_from,),
        )
    conn.commit()


def last_calculated_date(conn):
    row = conn.execute("select max(date_dt) from gen_internal.calc_log").fetchone()
    return row[0]


def load_params(conn) -> dict:
    rows = conn.execute("select key, value from gen_internal.params").fetchall()
    params = dict(DEFAULT_PARAMS)
    params.update({k: v for k, v in rows})
    return params


def save_params(conn, params: dict) -> None:
    with conn.cursor() as cur:
        for key, value in params.items():
            cur.execute(
                "insert into gen_internal.params (key, value) values (%s, %s) "
                "on conflict (key) do update set value = excluded.value",
                (key, str(value)),
            )
    conn.commit()


def user_supplier_ids(params: dict) -> set[int]:
    raw = params.get("user_suppliers", "")
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()}
