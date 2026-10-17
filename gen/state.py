"""Загрузка справочников и профилей в память для расчёта."""

from dataclasses import dataclass, field

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy connectable")

from gen.world import season_for


@dataclass
class World:
    products: pd.DataFrame          # индекс sku_id
    sku_ids: np.ndarray
    sku_index: dict                 # sku_id -> позиция в массивах
    profiles: pd.DataFrame          # индекс sku_id
    sku_info: dict                  # sku_id -> кортеж атрибутов (быстрый доступ)
    profile_info: dict              # sku_id -> кортеж параметров спроса
    customers: pd.DataFrame         # индекс customer_id
    sku_customers: dict             # sku_id -> (customer_ids, weights)
    suppliers: pd.DataFrame         # индекс supplier_id
    supplier_info: dict             # supplier_id -> (plan_days, stable, lead_sigma, lead_bias, short_prob)
    schedule: dict                  # supplier_id -> отсортированный список дат заказов
    schedule_set: dict              # supplier_id -> множество дат заказов
    supplier_skus: dict             # supplier_id -> [sku_id]
    cross_groups: dict              # cross_group_id -> [sku_id]
    sku_cross: dict                 # sku_id -> cross_group_id
    cross_share: dict               # sku_id -> base_share
    anomalies: dict                 # year -> (shift_months, winter_factor)
    season: dict = field(default_factory=dict)  # root_category_id -> массив из 12


def load_world(conn) -> World:
    products = pd.read_sql(
        """
        select p.sku_id, p.category_id,
               case when c.category_lvl = 1 then c.category_id
                    else coalesce(c2.parent_id, c.parent_id) end as root_category_id,
               p.supplier_id,
               p.price_segment, p.pack_size, p.price_inc, p.price
        from dim_products p
        join dim_category c on c.category_id = p.category_id
        left join dim_category c2 on c2.category_id = c.parent_id
        """,
        conn,
    ).set_index("sku_id")
    profiles = pd.read_sql("select * from gen_internal.sku_profile", conn).set_index("sku_id")
    customers = pd.read_sql("select * from gen_internal.customer_profile", conn).set_index("customer_id")

    sku_customers: dict[int, tuple] = {}
    for sku_id, customer_id, weight in conn.execute(
        "select sku_id, customer_id, weight from gen_internal.sku_customer order by sku_id"
    ).fetchall():
        ids, weights = sku_customers.setdefault(sku_id, ([], []))
        ids.append(customer_id)
        weights.append(float(weight))
    sku_customers = {k: (np.array(v[0]), np.array(v[1])) for k, v in sku_customers.items()}

    suppliers = pd.read_sql("select * from dim_supplier", conn).set_index("supplier_id")
    supplier_profiles = pd.read_sql(
        "select * from gen_internal.supplier_profile", conn
    ).set_index("supplier_id")

    schedule: dict[int, list] = {}
    for supplier_id, order_dt in conn.execute(
        "select supplier_id, order_dt from supplier_schedule order by supplier_id, order_dt"
    ).fetchall():
        schedule.setdefault(supplier_id, []).append(order_dt)

    cross_groups: dict[int, list] = {}
    sku_cross: dict[int, int] = {}
    for group_id, sku_id in conn.execute(
        "select cross_group_id, sku_id from dim_product_crosses"
    ).fetchall():
        cross_groups.setdefault(group_id, []).append(sku_id)
        sku_cross[sku_id] = group_id
    cross_share = dict(conn.execute(
        "select sku_id, base_share from gen_internal.cross_share"
    ).fetchall())

    anomalies = {
        year: (shift, float(winter))
        for year, shift, winter in conn.execute(
            "select year, shift_months, winter_factor from gen_internal.year_anomaly"
        ).fetchall()
    }

    sku_ids = products.index.to_numpy()
    sku_info = {
        int(sku): (int(row.category_id), int(row.root_category_id), int(row.supplier_id),
                   int(row.price_segment), int(row.pack_size), float(row.price_inc), float(row.price))
        for sku, row in products.iterrows()
    }
    profile_info = {
        int(sku): (int(row.root_category_id), row.demand_class, float(row.base_daily),
                   float(row.drift_rate), row.lifecycle, float(row.shock_prob),
                   float(row.promo_elasticity), row.abc_class)
        for sku, row in profiles.iterrows()
    }
    supplier_info = {}
    for supplier_id, row in suppliers.iterrows():
        sp = supplier_profiles.loc[supplier_id]
        supplier_info[int(supplier_id)] = (
            int(row.plan_income_day_cnt), bool(sp.stable), float(sp.lead_sigma),
            float(sp.lead_bias), float(sp.short_prob),
        )
    supplier_skus: dict[int, list] = {}
    for sku, info in sku_info.items():
        supplier_skus.setdefault(info[2], []).append(sku)

    world = World(
        products=products,
        sku_ids=sku_ids,
        sku_index={int(s): i for i, s in enumerate(sku_ids)},
        profiles=profiles,
        sku_info=sku_info,
        profile_info=profile_info,
        customers=customers,
        sku_customers=sku_customers,
        suppliers=suppliers,
        supplier_info=supplier_info,
        schedule=schedule,
        schedule_set={s: set(d) for s, d in schedule.items()},
        supplier_skus=supplier_skus,
        cross_groups=cross_groups,
        sku_cross=sku_cross,
        cross_share={int(k): float(v) for k, v in cross_share.items()},
        anomalies=anomalies,
    )
    for root in products["root_category_id"].unique():
        world.season[int(root)] = np.array(season_for(int(root)))
    return world
