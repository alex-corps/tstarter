"""Скрытый мир генератора: профили SKU, клиентов и поставщиков.

Профили создаются один раз и хранятся в gen_internal, чтобы модель спроса
не менялась между запусками. Повторный вызов ничего не пересоздаёт.
"""

import numpy as np

from gen.config import WORLD_SEED

# сезонный профиль по месяцам (январь..декабрь), сумма = 12
SEASON = {
    51:  [1.90, 1.75, 1.40, 1.05, 0.80, 0.60, 0.55, 0.65, 0.85, 1.05, 1.45, 1.80],  # стартеры
    1:   [1.25, 1.20, 1.05, 0.95, 0.90, 0.85, 0.85, 0.90, 1.00, 1.05, 1.10, 1.20],  # генераторы
    101: [0.70, 0.80, 1.00, 1.15, 1.25, 1.30, 1.35, 1.25, 1.05, 0.90, 0.75, 0.70],  # вентиляторы
    98:  [0.60, 0.65, 0.85, 1.10, 1.30, 1.45, 1.50, 1.40, 1.15, 0.90, 0.70, 0.60],  # компрессоры кондиционера
    170: [0.65, 0.70, 0.90, 1.15, 1.30, 1.40, 1.45, 1.35, 1.10, 0.90, 0.75, 0.65],  # кондиционирование
    118: [0.80, 0.85, 1.05, 1.20, 1.15, 1.05, 1.00, 0.95, 1.05, 1.10, 0.95, 0.85],  # фильтры (сезон ТО)
    140: [1.05, 1.05, 1.00, 1.00, 0.95, 0.90, 0.90, 0.95, 1.00, 1.05, 1.10, 1.05],  # двигатель
    200: [0.85, 0.90, 1.15, 1.25, 1.10, 0.95, 0.90, 0.90, 1.00, 1.10, 1.00, 0.90],  # подвеска
    129: [1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00],  # оборудование
    187: [1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00],  # крепёж
}
DEFAULT_SEASON = [1.0] * 12

# целевой уровень сервиса по ABC
SERVICE_Z = {"A": 1.65, "B": 1.28, "C": 0.84}

# ритм закупок по типу клиента: (интервал дней, размер заказа, разброс размера)
CUSTOMER_RHYTHM = {
    "СТО": (1.4, 1.3, 0.4),
    "розница": (6.0, 2.5, 0.5),
    "опт": (14.0, 8.0, 0.6),
    "автопарк": (5.0, 4.0, 0.35),
}


def season_for(root_category_id: int) -> list[float]:
    return SEASON.get(int(root_category_id), DEFAULT_SEASON)


def reset_world(conn) -> None:
    """Полностью пересоздаёт скрытый мир. Удаляет и рассчитанные данные."""
    with conn.cursor() as cur:
        for table in ("sku_profile", "customer_profile", "sku_customer", "supplier_profile",
                      "cross_share", "year_anomaly", "truth_demand", "calc_log", "backorder"):
            cur.execute(f"delete from gen_internal.{table}")
    conn.commit()
    ensure_world(conn)


def ensure_world(conn) -> None:
    exists = conn.execute("select 1 from gen_internal.sku_profile limit 1").fetchone()
    if exists:
        return
    rng = np.random.default_rng(WORLD_SEED)
    _build_sku_profiles(conn, rng)
    _build_customer_profiles(conn, rng)
    _build_sku_customers(conn, rng)
    _build_supplier_profiles(conn, rng)
    _build_cross_shares(conn, rng)
    _build_year_anomalies(conn, rng)
    conn.commit()


def _build_sku_profiles(conn, rng: np.random.Generator) -> None:
    rows = conn.execute(
        """
        select p.sku_id,
               case when c.category_lvl = 1 then c.category_id else coalesce(c2.parent_id, c.parent_id) end
        from dim_products p
        join dim_category c on c.category_id = p.category_id
        left join dim_category c2 on c2.category_id = c.parent_id
        order by p.sku_id
        """
    ).fetchall()
    sku_ids = np.array([r[0] for r in rows])
    roots = np.array([r[1] for r in rows])
    n = len(sku_ids)

    # парето-база даёт ABC-структуру: ~20% SKU дают ~80% спроса
    base = rng.pareto(1.3, n) + 0.05
    order = np.argsort(-base)
    abc = np.empty(n, dtype=object)
    abc[order[: int(n * 0.2)]] = "A"
    abc[order[int(n * 0.2) : int(n * 0.5)]] = "B"
    abc[order[int(n * 0.5) :]] = "C"

    # класс спроса: fast-movers среди ходовых, lumpy и intermittent среди редких
    rank = np.empty(n)
    rank[order] = np.linspace(0, 1, n)
    klass = np.empty(n, dtype=object)
    for i in range(n):
        roll = rng.random()
        if rank[i] < 0.25:
            klass[i] = "fast" if roll < 0.85 else "lumpy"
        elif rank[i] < 0.6:
            klass[i] = "intermittent" if roll < 0.6 else ("lumpy" if roll < 0.85 else "fast")
        else:
            klass[i] = "intermittent" if roll < 0.55 else "lumpy"

    # дневной спрос: fast — штуки в день, редкие — доли штуки в день
    daily = np.where(klass == "fast", 0.4 + base * 1.5, 0.01 + base * 0.15)

    lifecycle_roll = rng.random(n)
    lifecycle = np.where(lifecycle_roll < 0.15, "declining",
                 np.where(lifecycle_roll < 0.30, "growing", "stable"))
    drift = np.where(lifecycle == "declining", -rng.uniform(0.05, 0.25, n),
             np.where(lifecycle == "growing", rng.uniform(0.10, 0.45, n),
                      rng.normal(0, 0.02, n)))

    with conn.cursor() as cur:
        cur.executemany(
            """insert into gen_internal.sku_profile
               (sku_id, root_category_id, demand_class, base_daily, drift_rate,
                lifecycle, shock_prob, promo_elasticity, abc_class)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [
                (int(sku_ids[i]), int(roots[i]), klass[i], float(daily[i]), float(drift[i]),
                 lifecycle[i], float(rng.uniform(0.002, 0.02)),
                 float(rng.uniform(1.2, 2.5)), abc[i])
                for i in range(n)
            ],
        )


def _build_customer_profiles(conn, rng: np.random.Generator) -> None:
    rows = conn.execute(
        "select customer_id, customer_type from dim_customer order by customer_id"
    ).fetchall()
    out = []
    for customer_id, customer_type in rows:
        interval, size, jitter = CUSTOMER_RHYTHM[customer_type]
        out.append((
            customer_id, customer_type, customer_type,
            float(interval * rng.uniform(0.6, 1.6)),
            float(size * rng.uniform(0.5, 1.8)),
            float(jitter),
            "2024-01-01",
            None,
        ))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into gen_internal.customer_profile
               (customer_id, customer_type, profile_type, order_interval_days,
                order_size, size_jitter, activity_from, activity_to)
               values (%s,%s,%s,%s,%s,%s,%s,%s)""",
            out,
        )


def _build_sku_customers(conn, rng: np.random.Generator) -> None:
    """Каждый SKU покупает небольшая группа клиентов своего типа."""
    profiles = dict(conn.execute(
        "select sku_id, demand_class from gen_internal.sku_profile"
    ).fetchall())
    by_type = {}
    for customer_id, customer_type in conn.execute(
        "select customer_id, customer_type from dim_customer"
    ).fetchall():
        by_type.setdefault(customer_type, []).append(customer_id)
    for key in by_type:
        by_type[key] = np.array(by_type[key])

    # сколько клиентов на sku в зависимости от класса спроса
    buyers_by_class = {"fast": (25, 80), "intermittent": (2, 8), "lumpy": (1, 4)}
    out = []
    for sku_id, klass in profiles.items():
        lo, hi = buyers_by_class[klass]
        n_buyers = int(rng.integers(lo, hi + 1))
        if klass == "fast":
            pool = np.concatenate([by_type["СТО"], by_type["розница"]])
        elif klass == "lumpy":
            pool = np.concatenate([by_type["опт"], by_type["автопарк"]])
        else:
            pool = np.concatenate([by_type["СТО"], by_type["розница"], by_type["автопарк"]])
        chosen = rng.choice(pool, size=min(n_buyers, len(pool)), replace=False)
        weights = rng.uniform(0.3, 1.0, len(chosen))
        weights /= weights.sum()
        for customer_id, weight in zip(chosen, weights):
            out.append((int(sku_id), int(customer_id), float(weight)))

    with conn.cursor() as cur:
        cur.executemany(
            "insert into gen_internal.sku_customer (sku_id, customer_id, weight) values (%s,%s,%s)",
            out,
        )


def _build_supplier_profiles(conn, rng: np.random.Generator) -> None:
    rows = conn.execute("select supplier_id from dim_supplier order by supplier_id").fetchall()
    n = len(rows)
    # ~70% стабильных, ~30% с постоянными отклонениями
    stable_mask = rng.random(n) < 0.7
    out = []
    for i, (supplier_id,) in enumerate(rows):
        if stable_mask[i]:
            out.append((supplier_id, True, float(rng.uniform(0.4, 1.2)), 0.0,
                        float(rng.uniform(0.005, 0.02))))
        else:
            out.append((supplier_id, False, float(rng.uniform(2.0, 5.0)),
                        float(rng.uniform(1.0, 4.0)), float(rng.uniform(0.02, 0.05))))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into gen_internal.supplier_profile
               (supplier_id, stable, lead_sigma, lead_bias, short_prob) values (%s,%s,%s,%s,%s)""",
            out,
        )


def _build_cross_shares(conn, rng: np.random.Generator) -> None:
    rows = conn.execute(
        """
        select c.cross_group_id, c.sku_id, p.price_segment
        from dim_product_crosses c
        join dim_products p using (sku_id)
        order by c.cross_group_id, c.sku_id
        """
    ).fetchall()
    groups: dict[int, list] = {}
    for group_id, sku_id, segment in rows:
        groups.setdefault(group_id, []).append((sku_id, segment))
    out = []
    for group_id, members in groups.items():
        # дешёвый сегмент (3) получает большую долю спроса
        raw = np.array([rng.uniform(0.5, 1.5) * (1.4 if segment == 3 else 1.0)
                        for _, segment in members])
        raw /= raw.sum()
        for (sku_id, _), share in zip(members, raw):
            out.append((int(sku_id), int(group_id), float(share)))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into gen_internal.cross_share (sku_id, cross_group_id, base_share) values (%s,%s,%s)",
            out,
        )


def _build_year_anomalies(conn, rng: np.random.Generator) -> None:
    out = []
    for year in range(2024, 2032):
        shift = int(rng.integers(-1, 2))
        # примерно раз в четыре года тёплая зима снижает зимний пик
        winter = float(rng.uniform(0.7, 0.85)) if rng.random() < 0.25 else 1.0
        out.append((year, shift, winter))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into gen_internal.year_anomaly (year, shift_months, winter_factor) values (%s,%s,%s)",
            out,
        )
