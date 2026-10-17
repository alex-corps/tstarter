"""Приходы от поставщиков.

Приход создаётся только по ранее созданному заказу. Дата прихода назначается
в момент заказа и хранится в gen_internal.backorder.resolve_dt.
"""

import datetime as dt

import numpy as np

from gen.config import lead_sigma_factor, short_prob_factor
from gen.db import copy_rows


def plan_arrival(rng: np.random.Generator, world, supplier_id: int, order_date: dt.date,
                 plan_days: int, lead_level: int) -> dt.date:
    """Фактическая дата прихода: плановый срок плюс отклонение по профилю поставщика."""
    _, stable, lead_sigma, lead_bias, _ = world.supplier_info[supplier_id]
    factor = lead_sigma_factor(lead_level)
    sigma = lead_sigma * factor
    if stable:
        delta = rng.normal(0, sigma)
    else:
        delta = lead_bias * factor + rng.lognormal(0, sigma / 3) - 1
    days = max(1, int(round(plan_days + delta)))
    return order_date + dt.timedelta(days=days)


def receive_day(conn, world, rng: np.random.Generator, day: dt.date, stock: dict[int, int],
                transit: dict[int, int], short_level: int) -> dict:
    """Принимает на склад заказы, у которых наступила дата прихода."""
    due = conn.execute(
        """
        select supplier_id, sku_id, order_dt, order_qnty, open_qnty
        from gen_internal.backorder
        where resolve_dt = %s and open_qnty > 0
        """,
        (day,),
    ).fetchall()

    rows = []
    received = {}
    for supplier_id, sku_id, order_dt, order_qnty, open_qnty in due:
        prob = min(0.5, world.supplier_info[supplier_id][4] * short_prob_factor(short_level))
        income_qnty = int(open_qnty)
        still_open = 0
        if open_qnty > 1 and rng.random() < prob:
            income_qnty = max(1, int(open_qnty * rng.uniform(0.5, 0.95)))
            still_open = int(open_qnty) - income_qnty

        first_time = conn.execute(
            "select 1 from income_day where sku_id = %s and is_new limit 1", (sku_id,)
        ).fetchone() is None and stock.get(sku_id, 0) == 0
        lead_days = (day - order_dt).days
        price_inc = world.sku_info[sku_id][5]

        rows.append((supplier_id, day, sku_id, int(order_qnty), income_qnty,
                     price_inc, order_dt, lead_days, first_time))
        stock[sku_id] = stock.get(sku_id, 0) + income_qnty
        transit[sku_id] = max(0, transit.get(sku_id, 0) - income_qnty)
        received[sku_id] = received.get(sku_id, 0) + income_qnty

        if still_open:
            # довоз отдельным приходом через несколько дней
            again = day + dt.timedelta(days=int(rng.integers(3, 15)))
            conn.execute(
                "update gen_internal.backorder set open_qnty = %s, resolve_dt = %s "
                "where supplier_id = %s and sku_id = %s and order_dt = %s",
                (still_open, again, supplier_id, sku_id, order_dt),
            )
        else:
            conn.execute(
                "update gen_internal.backorder set open_qnty = 0 "
                "where supplier_id = %s and sku_id = %s and order_dt = %s",
                (supplier_id, sku_id, order_dt),
            )

    copy_rows(conn, "income_day",
              ["supplier_id", "date_dt", "sku_id", "order_qnty", "income_qnty",
               "price_inc", "income_order_dt", "income_day_cnt", "is_new"],
              rows)
    return {"rows": len(rows), "received": received}
