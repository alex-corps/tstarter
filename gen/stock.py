"""Подневные остатки."""

from gen.db import copy_rows


def write_rest_day(conn, day, stock: dict[int, int], transit: dict[int, int]) -> None:
    """Пишет остаток на конец дня и количество в пути по всем SKU с движением."""
    sku_ids = set(stock) | set(transit)
    rows = [
        (day, sku, int(stock.get(sku, 0)), int(transit.get(sku, 0)))
        for sku in sku_ids
        if stock.get(sku, 0) or transit.get(sku, 0)
    ]
    copy_rows(conn, "rest_day", ["date_dt", "sku_id", "rest_qnty", "rest_transit_qnty"], rows)


def load_last_stock(conn, day) -> tuple[dict, dict]:
    """Остатки и товар в пути на конец предыдущего рассчитанного дня."""
    row = conn.execute(
        "select max(date_dt) from rest_day where date_dt < %s", (day,)
    ).fetchone()
    if row[0] is None:
        return {}, {}
    stock, transit = {}, {}
    for sku_id, rest, in_transit in conn.execute(
        "select sku_id, rest_qnty, rest_transit_qnty from rest_day where date_dt = %s",
        (row[0],),
    ).fetchall():
        stock[sku_id] = int(rest)
        transit[sku_id] = int(in_transit)
    return stock, transit
