"""Первый запуск: стартовый остаток по виртуальным продажам за 30 дней.

Виртуальные продажи в базу не пишутся. Они нужны только чтобы оценить спрос
и положить на склад начальный запас до даты начала расчёта.
"""

import datetime as dt

import numpy as np

from gen.demand import DemandModel
from gen.stock import write_rest_day


def bootstrap(conn, world, date_from: dt.date, params: dict) -> None:
    """Создаёт остатки на день перед началом расчёта, если данных ещё нет."""
    exists = conn.execute("select 1 from rest_day limit 1").fetchone()
    if exists:
        return

    model = DemandModel(world, int(params["promo_level"]))
    start = date_from - dt.timedelta(days=30)
    status = dict(conn.execute(
        "select sku_id, status from current_assortment where date_to_dt is null"
    ).fetchall())

    totals: dict[int, int] = {sku: 0 for sku in status}
    for offset in range(30):
        day = start + dt.timedelta(days=offset)
        for sku_id in status:
            lam = model.sku_lambda(sku_id, day, 1.0)
            totals[sku_id] += int(model.rng.poisson(lam)) if lam > 0 else 0

    stock = {}
    for sku_id, total in totals.items():
        pack = max(1, world.sku_info[sku_id][4])
        # запас на ~45 дней продаж, но не меньше одного кванта упаковки
        cover = int(np.ceil(total * 45 / 30))
        stock[sku_id] = max(pack, int(np.ceil(cover / pack) * pack))

    write_rest_day(conn, date_from - dt.timedelta(days=1), stock, {})
    conn.commit()
