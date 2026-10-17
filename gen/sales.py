"""Продажи за день: спрос ограничивается остатком на начало дня.

Строки пишутся в sale_order_day: одна строка на (день, клиент, sku).
Истинный спрос и потерянные продажи сохраняются в gen_internal.truth_demand.
"""

import datetime as dt

import numpy as np

from gen.db import copy_rows


def sell_day(conn, world, model, day, stock: dict[int, int], active_skus: list[int],
             status_by_sku: dict[int, str], pending_returns: list) -> dict:
    """Возвращает проданные количества по sku и обновляет stock на месте."""
    sold_by_sku: dict[int, int] = {}
    sale_rows = []
    truth_rows = []
    lost_skus = 0

    # доступность аналогов: при OOS одного аналога спрос частично перетекает
    available = {sku for sku in active_skus if stock.get(sku, 0) > 0}

    for sku_id in active_skus:
        phasing_out = status_by_sku.get(sku_id) == "вывод"
        model.maybe_start_promo(sku_id, day, phasing_out)

        group = world.sku_cross.get(sku_id)
        available_share = 1.0
        if group is not None:
            members = [m for m in world.cross_groups[group] if m in status_by_sku]
            if members and sku_id not in available:
                # спроса нет, его доля уйдёт доступным аналогам
                available_share = 0.0
            elif members:
                present = sum(model.shares.get(m, 0.0) for m in members if m in available)
                own = model.shares.get(sku_id, 0.0)
                # 60% доли отсутствующих аналогов перетекает на доступные
                missing = sum(model.shares.get(m, 0.0) for m in members if m not in available)
                available_share = (own + 0.6 * missing * (own / present)) / own if own else 1.0

        lam = model.sku_lambda(sku_id, day, available_share)
        demand_qty = model.order_batch(sku_id, lam)
        if demand_qty == 0:
            continue
        demand = model.split_by_customers(sku_id, demand_qty)

        on_hand = stock.get(sku_id, 0)
        pending_back = _pending_back(pending_returns, day, sku_id)
        demand = _drop_cancelled(demand, pending_back)
        demand_qty = sum(demand.values())
        if demand_qty == 0:
            continue
        discount = model.active_discount(sku_id, day) or 0.0
        price_inc = world.sku_info[sku_id][5]
        price = world.sku_info[sku_id][6]

        served = demand
        sold_qty = demand_qty
        if demand_qty > on_hand:
            served, sold_qty = _allocate(model.rng, demand, on_hand)
            lost_skus += 1

        stock[sku_id] = on_hand - sold_qty
        sold_by_sku[sku_id] = sold_qty

        for customer_id, qty in served.items():
            sale_rows.append((
                day, int(customer_id), int(sku_id), int(qty),
                round(qty * price * (1 - discount), 2),
                round(qty * price_inc, 2),
                round(discount, 2),
            ))
        truth_rows.append((
            day, int(sku_id), int(demand_qty), int(sold_qty),
            int(demand_qty - sold_qty), discount > 0, round(discount, 2),
        ))

    due = [r for r in pending_returns if r[0] == day]
    pending_returns[:] = [r for r in pending_returns if r[0] != day]
    # возврат увеличивает остаток до продаж, иначе продажа того же дня его не увидит
    for _, _, sku_id, qty, *_ in due:
        stock[sku_id] = stock.get(sku_id, 0) - qty
    sale_rows = _merge(due + sale_rows)
    _schedule_artifacts(model.rng, day, sale_rows, pending_returns)

    copy_rows(conn, "sale_order_day",
              ["date_dt", "customer_id", "sku_id", "qnty", "price_sum", "price_inc_sum", "discount"],
              sale_rows)
    copy_rows(conn, "gen_internal.truth_demand",
              ["date_dt", "sku_id", "demand_qnty", "sold_qnty", "lost_qnty", "promo", "discount"],
              truth_rows)
    return {
        "sold": sold_by_sku,
        "sales_rows": len(sale_rows),
        "oos_skus": lost_skus,
    }


def _allocate(rng, demand: dict[int, int], on_hand: int):
    """Раздаёт остаток клиентам в случайном порядке."""
    ids = list(demand.keys())
    rng.shuffle(ids)
    served = {}
    left = on_hand
    for customer_id in ids:
        if left <= 0:
            break
        qty = min(demand[customer_id], left)
        served[customer_id] = qty
        left -= qty
    return served, on_hand - left


def _merge(rows: list) -> list:
    """Складывает строки с одинаковым клиентом и SKU, чтобы не нарушить ключ."""
    acc: dict[tuple, list] = {}
    for date_dt, customer_id, sku_id, qty, price_sum, price_inc_sum, discount in rows:
        key = (customer_id, sku_id)
        if key in acc:
            prev = acc[key]
            prev[3] += qty
            prev[4] = round(prev[4] + price_sum, 2)
            prev[5] = round(prev[5] + price_inc_sum, 2)
        else:
            acc[key] = [date_dt, customer_id, sku_id, qty, price_sum, price_inc_sum, discount]
    return [tuple(v) for v in acc.values() if v[3] != 0]


def _pending_back(pending: list, day, sku_id: int) -> dict[int, int]:
    """Возвраты этого SKU, которые придут сегодня, по клиентам."""
    out: dict[int, int] = {}
    for when, customer_id, sku, qty, *_ in pending:
        if when == day and sku == sku_id:
            out[customer_id] = out.get(customer_id, 0) - qty
    return out


def _drop_cancelled(demand: dict[int, int], pending_back: dict[int, int]) -> dict[int, int]:
    """Убирает из продажи количество, которое сегодня же вернётся тем же клиентом."""
    if not pending_back:
        return demand
    out = {}
    for customer_id, qty in demand.items():
        qty -= pending_back.get(customer_id, 0)
        if qty > 0:
            out[customer_id] = qty
    return out


def _returned_qty(due: list, merged: list) -> dict[int, int]:
    """Сколько реально вернулось на склад.

    Возврат, схлопнутый с продажей того же клиента и SKU, остаток не увеличивает:
    продажа уже уменьшила его на большее количество.
    """
    net_negative = {}
    for _, customer_id, sku_id, qty, *_ in merged:
        if qty < 0:
            net_negative[(customer_id, sku_id)] = -qty
    out: dict[int, int] = {}
    for _, customer_id, sku_id, qty, *_ in due:
        back = net_negative.get((customer_id, sku_id), 0)
        if back:
            out[sku_id] = out.get(sku_id, 0) + back
    return out


def _schedule_artifacts(rng: np.random.Generator, day, sale_rows: list, pending: list) -> None:
    """Планирует сторно задвоения и возврат брака на следующие дни.

    Отрицательное количество вернётся на склад в день сторно.
    """
    positives = [r for r in sale_rows if r[3] > 0]
    if not positives or rng.random() > 0.15:
        return
    src = positives[int(rng.integers(0, len(positives)))]
    _, customer_id, sku_id, qty, price_sum, price_inc_sum, discount = src
    back = max(1, int(qty * rng.uniform(0.3, 1.0)))
    back = min(back, qty)
    when = day + dt.timedelta(days=int(rng.integers(10, 21)))
    pending.append((when, customer_id, sku_id, -back,
                    round(-abs(price_sum) * back / qty, 2),
                    round(-abs(price_inc_sum) * back / qty, 2), discount))
