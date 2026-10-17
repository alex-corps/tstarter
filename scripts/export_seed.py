"""Выгружает справочники в data/seed/*.csv.

Факты (продажи, заказы, остатки) не выгружаются: их создаёт генератор.
Ассортимент выгружается в исходном виде: 4000 активных SKU с 2024-01-01.
"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gen.db import connect  # noqa: E402

OUT = ROOT / "data" / "seed"

# (таблица, колонки, sql)
EXPORTS = [
    ("dim_category", "category_id, parent_id, category_lvl, category_name",
     "select category_id, parent_id, category_lvl, category_name from dim_category order by category_id"),
    ("dim_supplier", "supplier_id, contractor_name, plan_income_day_cnt",
     "select supplier_id, contractor_name, plan_income_day_cnt from dim_supplier order by supplier_id"),
    ("dim_customer", "customer_id, customer_type, customer_name, profile_type",
     "select customer_id, customer_type, customer_name, profile_type from dim_customer order by customer_id"),
    ("dim_products", "sku_id, category_id, supplier_id, brand_name, price_segment, pack_size, price_inc, price",
     "select sku_id, category_id, supplier_id, brand_name, price_segment, pack_size, price_inc, price "
     "from dim_products order by sku_id"),
    ("dim_product_crosses", "cross_group_id, sku_id",
     "select cross_group_id, sku_id from dim_product_crosses order by cross_group_id, sku_id"),
    ("supplier_schedule", "supplier_id, order_dt, plan_income_day_cnt",
     "select supplier_id, order_dt, plan_income_day_cnt from supplier_schedule order by supplier_id, order_dt"),
    ("current_assortment", "sku_id, status, date_from_dt, date_to_dt",
     "select sku_id, 'активный', date '2024-01-01', null "
     "from current_assortment where date_from_dt = date '2024-01-01' order by sku_id"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect()
    for table, header, sql in EXPORTS:
        rows = conn.execute(sql).fetchall()
        path = OUT / f"{table}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header.split(", "))
            writer.writerows(rows)
        print(f"{table}: {len(rows)}")


if __name__ == "__main__":
    main()
