"""Заказы поставщикам: периодическое пополнение до целевого уровня (R, S).

Прогноз строится по продажам, очищенным от дней OOS и распродаж.
Новинки заказываются по статистике аналога (ротация) или опорного разреза (расширение).
"""

import datetime as dt

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy connectable")

from gen.config import oos_cycle_prob
from gen.db import copy_rows
from gen.receipts import plan_arrival
from gen.world import SERVICE_Z


class History:
    """История продаж по SKU за скользящие 120 дней."""

    WINDOW = 120

    def __init__(self, sku_index: dict):
        n = len(sku_index)
        self.sku_index = sku_index
        self.sold = np.zeros((n, self.WINDOW), dtype=np.int32)
        self.promo = np.zeros((n, self.WINDOW), dtype=bool)
        self.on_hand = np.ones((n, self.WINDOW), dtype=bool)
        self.pos = 0
        self.filled = 0

    def add_day(self, conn, day: dt.date) -> None:
        rows = conn.execute(
            "select sku_id, sold_qnty, promo, lost_qnty from gen_internal.truth_demand where date_dt = %s",
            (day,),
        ).fetchall()
        col = self.pos
        self.sold[:, col] = 0
        self.promo[:, col] = False
        self.on_hand[:, col] = True
        for sku_id, sold_qnty, is_promo, lost in rows:
            i = self.sku_index.get(sku_id)
            if i is None:
                continue
            self.sold[i, col] = sold_qnty
            self.promo[i, col] = is_promo
            self.on_hand[i, col] = lost == 0
        self.pos = (self.pos + 1) % self.WINDOW
        self.filled = min(self.filled + 1, self.WINDOW)

    def clean_series(self, sku_id: int):
        """Продажи без дней распродаж и без дней OOS."""
        i = self.sku_index.get(sku_id)
        if i is None or self.filled == 0:
            return np.empty(0)
        cols = (np.arange(self.filled) + self.pos - self.filled) % self.WINDOW
        mask = ~self.promo[i, cols] & self.on_hand[i, cols]
        return self.sold[i, cols][mask].astype(float)

    def mean_sold(self, sku_id: int) -> float:
        i = self.sku_index.get(sku_id)
        if i is None or self.filled == 0:
            return 0.0
        cols = (np.arange(self.filled) + self.pos - self.filled) % self.WINDOW
        return float(self.sold[i, cols].mean())


def order_day(conn, world, model, day: dt.date, stock: dict[int, int], transit: dict[int, int],
              status: dict[int, str], user_suppliers: set[int], oos_level: int, lead_level: int,
              history: "History") -> int:
    """Формирует заказы поставщикам, у которых сегодня день заказа. Возвращает число строк."""
    due_suppliers = [s for s, dates in world.schedule_set.items()
                     if day in dates and s not in user_suppliers]
    if not due_suppliers:
        return 0

    rows = []
    backorders = []
    for supplier_id in due_suppliers:
        plan_days = _plan_days(world, supplier_id, day)
        review = _review_interval(world, supplier_id, day)
        skus = [s for s in world.supplier_skus.get(supplier_id, [])
                if status.get(s) in ("активный", "ввод")]
        for sku_id in skus:
            qty = _order_qty(world, model, day, sku_id, status, stock, transit,
                             history, plan_days, review, oos_level)
            if qty <= 0:
                continue
            rows.append((day, supplier_id, sku_id, int(qty)))
            arrival = plan_arrival(model.rng, world, supplier_id, day, plan_days, lead_level)
            backorders.append((supplier_id, sku_id, day, int(qty), int(qty), arrival))
            transit[sku_id] = transit.get(sku_id, 0) + int(qty)

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


def _order_qty(world, model, day, sku_id, status, stock, transit, history,
               plan_days, review, oos_level) -> int:
    pack = max(1, world.sku_info[sku_id][4])
    profile = world.profile_info[sku_id]
    st = status.get(sku_id)

    daily = _forecast_daily(world, sku_id, st, history, profile)
    if daily <= 0:
        return 0

    abc = profile[7]
    z = SERVICE_Z.get(abc, 1.0)
    # вариация спроса: для редкого спроса берём пуассоновскую, для частого — из истории
    sigma_d = max(float(np.sqrt(daily)), daily * 0.35)
    sigma_l = world.supplier_info[world.sku_info[sku_id][2]][2]
    ss = z * np.sqrt(plan_days * sigma_d ** 2 + daily ** 2 * sigma_l ** 2)

    # провоцирование OOS: с вероятностью, заданной параметром, страховой запас обнуляется
    if model.rng.random() < oos_cycle_prob(oos_level):
        ss = 0.0

    target = daily * (review + plan_days) + ss
    position = stock.get(sku_id, 0) + transit.get(sku_id, 0)
    if position >= target:
        return 0
    qty = int(np.ceil((target - position) / pack) * pack)
    return max(qty, pack)


def _forecast_daily(world, sku_id, status, history: History, profile) -> float:
    """Дневной прогноз, очищенный от распродаж и дней без остатка."""
    if status == "ввод":
        return _novelty_forecast(world, sku_id, history)
    sold = history.clean_series(sku_id)
    if sold.size == 0:
        return profile[2] * 0.5
    if profile[1] == "fast":
        return _ses(sold)
    return _croston_sba(sold)


def _novelty_forecast(world, sku_id, history: History) -> float:
    """Ротация: спрос аналога. Расширение: средняя SKU опорного разреза."""
    group = world.sku_cross.get(sku_id)
    if group is not None:
        for member in world.cross_groups[group]:
            rate = history.mean_sold(member)
            if rate > 0:
                return rate
    root = world.profile_info[sku_id][0]
    cols = (np.arange(history.filled) + history.pos - history.filled) % history.WINDOW
    idx = [i for sku, i in history.sku_index.items() if world.profile_info[sku][0] == root]
    rates = history.sold[idx][:, cols].mean(axis=1)
    rates = rates[rates > 0]
    if rates.size == 0:
        return world.profile_info[sku_id][2] * 0.6
    return float(np.median(rates)) * 0.6


def _ses(values: np.ndarray, alpha: float = 0.3) -> float:
    level = values[0]
    for v in values[1:]:
        level = alpha * v + (1 - alpha) * level
    return float(max(level, 0))


def _croston_sba(values: np.ndarray, alpha: float = 0.2) -> float:
    """Croston со сглаживанием Syntetos-Boylan для прерывистого спроса."""
    nonzero = np.flatnonzero(values > 0)
    if len(nonzero) == 0:
        return 0.0
    demand = values[nonzero[0]]
    interval = float(nonzero[0] + 1)
    prev = nonzero[0]
    for idx in nonzero[1:]:
        demand = alpha * values[idx] + (1 - alpha) * demand
        interval = alpha * (idx - prev) + (1 - alpha) * interval
        prev = idx
    if interval <= 0:
        return 0.0
    return float((1 - alpha / 2) * demand / interval)


def _plan_days(world, supplier_id: int, day: dt.date) -> int:
    return world.supplier_info[supplier_id][0]


def _review_interval(world, supplier_id: int, day: dt.date) -> int:
    dates = [d for d in world.schedule[supplier_id]]
    future = [d for d in dates if d > day]
    if future:
        return max(1, (future[0] - day).days)
    if len(dates) >= 2:
        return max(1, (dates[-1] - dates[-2]).days)
    return 14
