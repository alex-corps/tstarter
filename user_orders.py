"""Расчёт заказов поставщикам в режиме user.

Генератор вызывает calc_orders для каждого поставщика из списка user
в день его заказа по графику. Замените тело функции своим расчётом:
здесь только пример на скользящем среднем.

Возвращаемый DataFrame: колонки sku_id, qnty.
"""

import datetime as dt

import pandas as pd


def calc_orders(conn, supplier_id: int, calc_date: dt.date) -> pd.DataFrame:
    """Потребность = средние продажи за 30 дней * (срок поставки + интервал) - остаток - в пути."""
    window_from = calc_date - dt.timedelta(days=30)
    sales = pd.read_sql(
        """
        select d.sku_id, sum(d.qnty) as sold
        from sale_order_day d
        join dim_products p on p.sku_id = d.sku_id
        where p.supplier_id = %(supplier)s
          and d.date_dt >= %(start)s and d.date_dt < %(day)s
        group by d.sku_id
        """,
        conn, params={"supplier": supplier_id, "start": window_from, "day": calc_date},
    )
    if sales.empty:
        return pd.DataFrame(columns=["sku_id", "qnty"])

    rest = pd.read_sql(
        """
        select r.sku_id, r.rest_qnty, r.rest_transit_qnty
        from rest_day r
        join dim_products p on p.sku_id = r.sku_id
        where p.supplier_id = %(supplier)s
          and r.date_dt = (select max(date_dt) from rest_day where date_dt < %(day)s)
        """,
        conn, params={"supplier": supplier_id, "day": calc_date},
    )
    lead = conn.execute(
        "select plan_income_day_cnt from dim_supplier where supplier_id = %s",
        (supplier_id,),
    ).fetchone()[0]

    merged = sales.merge(rest, on="sku_id", how="left").fillna(0)
    cover_days = int(lead) + 14
    need = (merged.sold / 30 * cover_days - merged.rest_qnty - merged.rest_transit_qnty)
    merged["qnty"] = need.clip(lower=0).round().astype(int)
    return merged.loc[merged.qnty > 0, ["sku_id", "qnty"]].reset_index(drop=True)


def manage_assortment(conn, supplier_id: int, calc_date: dt.date) -> None:
    """Управление ассортиментом поставщика. По умолчанию ничего не меняет."""
    return None
