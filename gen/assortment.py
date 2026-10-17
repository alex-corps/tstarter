"""Текущий ассортимент: статусы, ротация и расширение.

Раз в месяц (в первый день заказа месяца) часть SKU выводится, а на их место
и в дополнение вводятся новые. Кандидаты берутся из пула SKU, которых нет
в ассортименте.
"""

import datetime as dt


def load_assortment(conn, day: dt.date) -> dict[int, str]:
    """Действующий статус SKU на дату."""
    rows = conn.execute(
        """
        select sku_id, status from current_assortment
        where date_from_dt <= %s and (date_to_dt is null or date_to_dt >= %s)
        """,
        (day, day),
    ).fetchall()
    return {sku_id: status for sku_id, status in rows}


def activate_arrived(conn, day: dt.date, received: dict[int, int]) -> int:
    """SKU в статусе «ввод» переходит в «активный» при первом приходе на склад."""
    if not received:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            update current_assortment set status = 'активный'
            where status = 'ввод' and sku_id = any(%s)
              and date_from_dt <= %s and date_to_dt is null
            """,
            (list(received), day),
        )
        return cur.rowcount


def close_depleted(conn, day: dt.date, stock: dict[int, int]) -> int:
    """Закрывает период действия выводимых SKU, у которых остаток вымылся в ноль."""
    rows = conn.execute(
        """
        select sku_id from current_assortment
        where status = 'вывод' and date_to_dt is null and date_from_dt <= %s
        """,
        (day,),
    ).fetchall()
    depleted = [sku_id for (sku_id,) in rows if stock.get(sku_id, 0) <= 0]
    if not depleted:
        return 0
    conn.execute(
        "update current_assortment set date_to_dt = %s where sku_id = any(%s) and date_to_dt is null",
        (day, depleted),
    )
    return len(depleted)


def rotate(conn, world, model, day: dt.date, status: dict[int, str], stock: dict[int, int],
           rotation_n: int, novelty_n: int, user_suppliers: set[int]) -> dict:
    """Ротация и расширение ассортимента. Возвращает обновлённый словарь статусов."""
    if day.day > 7:
        return status
    already = conn.execute(
        "select 1 from current_assortment where date_from_dt >= %s and date_from_dt < %s limit 1",
        (day.replace(day=1), day),
    ).fetchone()
    if already:
        return status

    pool = _pool(conn, world, status, user_suppliers)
    rng = model.rng

    # ротация: активный SKU выводится, его аналог из пула вводится
    active = [s for s, st in status.items() if st == "активный"
              and world.sku_info[s][2] not in user_suppliers]
    rng.shuffle(active)
    rotated = 0
    for sku_id in active:
        if rotated >= rotation_n:
            break
        group = world.sku_cross.get(sku_id)
        if group is None:
            continue
        candidates = [m for m in world.cross_groups[group] if m in pool]
        if not candidates:
            continue
        new_sku = candidates[0]
        _phase_out(conn, sku_id, day)
        _introduce(conn, new_sku, day)
        pool.discard(new_sku)
        status[sku_id] = "вывод"
        status[new_sku] = "ввод"
        model.set_share(new_sku, model.shares.get(sku_id, 0.5))
        rotated += 1

    # расширение: половина — аналог к действующему (сосуществуют), половина — новые SKU
    introduced = 0
    coexist_target = novelty_n // 2
    for sku_id in list(status):
        if introduced >= coexist_target:
            break
        group = world.sku_cross.get(sku_id)
        if group is None or status.get(sku_id) != "активный":
            continue
        candidates = [m for m in world.cross_groups[group] if m in pool]
        if not candidates:
            continue
        new_sku = candidates[0]
        _introduce(conn, new_sku, day)
        pool.discard(new_sku)
        status[new_sku] = "ввод"
        # делим долю пополам: аналоги работают вместе
        half = model.shares.get(sku_id, 1.0) / 2
        model.set_share(sku_id, half)
        model.set_share(new_sku, half)
        introduced += 1

    fresh = [s for s in pool if s not in world.sku_cross]
    rng.shuffle(fresh)
    for new_sku in fresh:
        if introduced >= novelty_n:
            break
        _introduce(conn, new_sku, day)
        status[new_sku] = "ввод"
        introduced += 1

    return status


def _pool(conn, world, status: dict[int, str], user_suppliers: set[int]) -> set[int]:
    used = {sku for (sku,) in conn.execute("select distinct sku_id from current_assortment")}
    return {
        int(sku) for sku in world.sku_info
        if int(sku) not in used and int(sku) not in status
        and world.sku_info[int(sku)][2] not in user_suppliers
    }


def _phase_out(conn, sku_id: int, day: dt.date) -> None:
    conn.execute(
        "update current_assortment set status = 'вывод', date_to_dt = null "
        "where sku_id = %s and date_to_dt is null",
        (sku_id,),
    )


def _introduce(conn, sku_id: int, day: dt.date) -> None:
    conn.execute(
        "insert into current_assortment (sku_id, status, date_from_dt, date_to_dt) "
        "values (%s, 'ввод', %s, null)",
        (sku_id, day),
    )
