"""Приходы от поставщиков.

Приход создаётся только по ранее созданному заказу. Дата назначается один раз
на весь заказ в момент заказа и хранится в gen_internal.backorder.resolve_dt.
Один заказ даёт один документ прихода и одну дату.
"""

import datetime as dt

import numpy as np

from gen.config import lead_sigma_factor, short_prob_factor
from gen.db import copy_rows


def plan_arrival(rng: np.random.Generator, world, supplier_id: int, order_date: dt.date,
                 plan_days: int, lead_level: int, recovery: bool = False) -> dt.date:
    """Фактическая дата прихода: плановый срок плюс отклонение по профилю поставщика.

    Вызывается один раз на заказ: все строки приходят в эту дату.
    recovery=True убирает длинный хвост и используется, только если так нужно
    повести весь заказ, а не отдельный SKU.
    """
    if recovery:
        days = max(1, plan_days + int(rng.integers(0, 3)))
        return order_date + dt.timedelta(days=days)
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
    """Принимает на склад заказы, у которых наступила дата прихода.

    На заказ пишется один income и строки income_detail. Недопоставка уменьшает
    количество и не назначает вторую дату.
    """
    due = conn.execute(
        """
        select income_order_id, supplier_id, sku_id, order_dt, order_qnty, open_qnty
        from gen_internal.backorder
        where resolve_dt = %s and open_qnty > 0
        order by income_order_id, sku_id
        """,
        (day,),
    ).fetchall()

    grouped: dict[int, dict] = {}
    for income_order_id, supplier_id, sku_id, order_dt, order_qnty, open_qnty in due:
        bucket = grouped.setdefault(income_order_id, {
            "supplier_id": supplier_id,
            "order_dt": order_dt,
            "lines": [],
        })
        bucket["lines"].append((sku_id, int(order_qnty), int(open_qnty)))

    already_new = set()
    if grouped:
        already_new = {
            sku_id for (sku_id,) in conn.execute("select sku_id from income_detail where is_new")
        }

    received = {}
    detail_count = 0
    closed = []
    for income_order_id, order in grouped.items():
        supplier_id = order["supplier_id"]
        order_dt = order["order_dt"]
        lead_days = (day - order_dt).days
        income_id = conn.execute(
            """
            insert into income (supplier_id, date_dt, income_day_cnt, income_order_id, income_order_dt)
            values (%s, %s, %s, %s, %s)
            returning income_id
            """,
            (supplier_id, day, lead_days, income_order_id, order_dt),
        ).fetchone()[0]

        prob = min(0.5, world.supplier_info[supplier_id][4] * short_prob_factor(short_level))
        details = []
        for sku_id, order_qnty, open_qnty in order["lines"]:
            income_qnty = open_qnty
            if open_qnty > 1 and rng.random() < prob:
                income_qnty = max(1, int(open_qnty * rng.uniform(0.5, 0.95)))
            first_time = sku_id not in already_new and stock.get(sku_id, 0) == 0
            if first_time:
                already_new.add(sku_id)
            details.append((
                income_id, sku_id, order_qnty, income_qnty, world.sku_info[sku_id][5], first_time,
            ))
            stock[sku_id] = stock.get(sku_id, 0) + income_qnty
            # недовезённый остаток не приедет отдельным приходом и не остаётся в пути
            transit[sku_id] = max(0, transit.get(sku_id, 0) - open_qnty)
            received[sku_id] = received.get(sku_id, 0) + income_qnty

        copy_rows(
            conn, "income_detail",
            ["income_id", "sku_id", "order_qnty", "income_qnty", "price_inc", "is_new"],
            details,
        )
        closed.append(income_order_id)
        detail_count += len(details)

    if closed:
        conn.execute(
            "update gen_internal.backorder set open_qnty = 0 where income_order_id = any(%s)",
            (closed,),
        )
    return {"rows": detail_count, "received": received}
