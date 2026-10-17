"""Дневной цикл генератора."""

import datetime as dt

from gen.assortment import activate_arrived, close_depleted, load_assortment, rotate
from gen.bootstrap import bootstrap
from gen.db import load_params, rollback_from, user_supplier_ids
from gen.demand import DemandModel
from gen.ordering import History, order_day
from gen.receipts import receive_day
from gen.sales import sell_day
from gen.state import load_world
from gen.stock import load_last_stock, write_rest_day
from user_orders import calc_orders


def _user_orders(conn, world, model, day, stock, transit, status, users, lead_level) -> int:
    """Заказы поставщиков в режиме user: берёт расчёт из user_orders.py."""
    from gen.db import copy_rows
    from gen.receipts import plan_arrival

    due = [s for s in users if day in world.schedule_set.get(s, ())]
    rows, backorders = [], []
    for supplier_id in due:
        frame = calc_orders(conn, supplier_id, day)
        plan_days = world.supplier_info[supplier_id][0]
        for sku_id, qty in frame.itertuples(index=False):
            sku_id, qty = int(sku_id), int(qty)
            if qty <= 0 or status.get(sku_id) == "вывод":
                continue
            rows.append((day, supplier_id, sku_id, qty))
            arrival = plan_arrival(model.rng, world, supplier_id, day, plan_days, lead_level)
            backorders.append((supplier_id, sku_id, day, qty, qty, arrival))
            transit[sku_id] = transit.get(sku_id, 0) + qty
    copy_rows(conn, "income_order_day", ["date_dt", "supplier_id", "sku_id", "qnty"], rows)
    with conn.cursor() as cur:
        cur.executemany(
            """insert into gen_internal.backorder
               (supplier_id, sku_id, order_dt, order_qnty, open_qnty, resolve_dt)
               values (%s,%s,%s,%s,%s,%s)
               on conflict (supplier_id, sku_id, order_dt) do nothing""",
            backorders,
        )
    return len(rows)


def run(conn, date_from: dt.date, date_to: dt.date, progress=None) -> dict:
    """Считает период включительно. progress(done, total) вызывается после каждого дня."""
    params = load_params(conn)
    rollback_from(conn, date_from)

    world = load_world(conn)
    bootstrap(conn, world, date_from, params)
    model = DemandModel(world, int(params["promo_level"]))
    users = user_supplier_ids(params)

    total_days = (date_to - date_from).days + 1
    pending_returns: list = []
    history = History(world.sku_index)
    totals = {"sales_rows": 0, "order_rows": 0, "income_rows": 0, "oos_skus": 0}

    for done in range(total_days):
        day = date_from + dt.timedelta(days=done)
        stock, transit = load_last_stock(conn, day)
        status = load_assortment(conn, day)
        active = [sku for sku, st in status.items() if st in ("активный", "вывод")]

        sales = sell_day(conn, world, model, day, stock, active, status, pending_returns)
        receipts = receive_day(conn, world, model.rng, day, stock, transit, int(params["short_level"]))
        activate_arrived(conn, day, receipts["received"])
        close_depleted(conn, day, stock)
        status = rotate(conn, world, model, day, status, stock,
                        int(params["rotation_per_month"]), int(params["novelty_per_month"]), users)
        history.add_day(conn, day)
        orders = order_day(conn, world, model, day, stock, transit, status, users,
                           int(params["oos_level"]), int(params["lead_level"]), history)
        orders += _user_orders(conn, world, model, day, stock, transit, status, users,
                               int(params["lead_level"]))
        write_rest_day(conn, day, stock, transit)

        conn.execute(
            """insert into gen_internal.calc_log (date_dt, sales_rows, order_rows, income_rows, oos_skus)
               values (%s,%s,%s,%s,%s)""",
            (day, sales["sales_rows"], orders, receipts["rows"], sales["oos_skus"]),
        )
        conn.commit()

        for key, value in (("sales_rows", sales["sales_rows"]), ("order_rows", orders),
                           ("income_rows", receipts["rows"]), ("oos_skus", sales["oos_skus"])):
            totals[key] += value
        if progress:
            progress(done + 1, total_days)

    return totals
