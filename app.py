"""Панель управления генератором данных."""

import datetime as dt

import streamlit as st

from gen.db import connect, last_calculated_date, load_params, save_params, user_supplier_ids
from gen.engine import run
from gen.world import ensure_world

st.set_page_config(page_title="Генератор данных", layout="wide")
st.title("Генератор данных B2B-дистрибьютора")

conn = connect()
ensure_world(conn)
params = load_params(conn)

suppliers = conn.execute(
    "select supplier_id, contractor_name from dim_supplier order by supplier_id"
).fetchall()
supplier_labels = {sid: f"{sid}. {name}" for sid, name in suppliers}


def next_user_order_day(today: dt.date) -> dt.date | None:
    """Ближайший день заказа по поставщикам в режиме user с активными SKU."""
    users = user_supplier_ids(params)
    if not users:
        return None
    row = conn.execute(
        """
        select min(s.income_order_dt)
        from supplier_schedule s
        where s.supplier_id = any(%s)
          and s.income_order_dt > %s
          and exists (
              select 1 from current_assortment a
              join dim_products p on p.sku_id = a.sku_id
              where p.supplier_id = s.supplier_id
                and a.status = 'активный'
                and a.date_from_dt <= s.income_order_dt
                and (a.date_to_dt is null or a.date_to_dt >= s.income_order_dt)
          )
        """,
        (list(users), today),
    ).fetchone()
    return row[0]


st.session_state.setdefault("date_from_text", "2025-01-01")
st.session_state.setdefault("date_to_text", "2025-03-31")


def _parse(text: str):
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError:
        return None


with st.sidebar:
    st.header("Период расчёта")
    date_from_text = st.text_input("Дата с (ГГГГ-ММ-ДД)", key="date_from_text")
    date_to_text = st.text_input("Дата по (ГГГГ-ММ-ДД)", key="date_to_text")
    date_from = _parse(date_from_text)
    date_to = _parse(date_to_text)
    if st.button("До дня расчёта"):
        base = date_from or dt.date.today()
        nxt = next_user_order_day(base)
        if nxt:
            st.session_state["date_to_text"] = nxt.isoformat()
            st.rerun()
        else:
            st.warning("Нет поставщиков в режиме user с активными SKU.")

    st.header("Параметры")
    oos_level = st.slider("Частота OOS", 1, 10, int(params["oos_level"]))
    promo_level = st.slider("Частота распродаж", 1, 10, int(params["promo_level"]))
    lead_level = st.slider("Вариативность поставок", 1, 10, int(params["lead_level"]))
    short_level = st.slider("Недопоставка", 1, 10, int(params["short_level"]))
    rotation = st.number_input("Ротация, SKU в месяц", 0, 100, int(params["rotation_per_month"]))
    novelty = st.number_input("Новинки, SKU в месяц", 0, 100, int(params["novelty_per_month"]))

    st.header("Режим user")
    current_users = user_supplier_ids(params)
    chosen = st.multiselect(
        "Поставщики, которых считаете вы",
        options=list(supplier_labels),
        default=sorted(current_users),
        format_func=lambda sid: supplier_labels[sid],
    )

last = last_calculated_date(conn)
st.caption(f"Последний рассчитанный день: {last or 'нет'}")

if st.button("Рассчитать", type="primary"):
    if date_from is None or date_to is None:
        st.error("Даты вводятся в формате ГГГГ-ММ-ДД.")
    elif date_to < date_from:
        st.error("Дата по раньше даты с.")
    else:
        save_params(conn, {
            "oos_level": oos_level,
            "promo_level": promo_level,
            "lead_level": lead_level,
            "short_level": short_level,
            "rotation_per_month": int(rotation),
            "novelty_per_month": int(novelty),
            "user_suppliers": ",".join(str(s) for s in chosen),
        })
        bar = st.progress(0, text="0 дней")

        def progress(done, total):
            bar.progress(done / total, text=f"{done} из {total} дней")

        totals = run(conn, date_from, date_to, progress)
        st.success("Готово")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Строк продаж", f"{totals['sales_rows']:,}".replace(",", " "))
        c2.metric("Строк заказов", f"{totals['order_rows']:,}".replace(",", " "))
        c3.metric("Строк приходов", f"{totals['income_rows']:,}".replace(",", " "))
        c4.metric("SKU-дней с OOS", f"{totals['oos_skus']:,}".replace(",", " "))

st.header("Сводка")
summary = conn.execute(
    """
    select (select count(*) from sale_order_day) as sales,
           (select count(*) from income_order_detail) as orders,
           (select count(*) from income_detail) as incomes,
           (select count(*) from gen_internal.truth_demand where promo) as promo_days,
           (select coalesce(sum(lost_qnty),0) from gen_internal.truth_demand) as lost
    """
).fetchone()
st.write(
    f"Продаж: **{summary[0]:,}**, заказов: **{summary[1]:,}**, приходов: **{summary[2]:,}**, "
    f"SKU-дней с распродажей: **{summary[3]:,}**, потеряно при OOS: **{summary[4]:,}** шт."
    .replace(",", " ")
)
conn.close()
