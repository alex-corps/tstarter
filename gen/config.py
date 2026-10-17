"""Подключение к БД и перевод шкалы параметров панели в вероятности.

Параметры читаются из окружения, а если их нет — из файла .env в корне проекта.
Переменные окружения важнее .env. Без настроек остаётся локальная база
localhost:5433 / postgres / rekill.
"""

import os
from pathlib import Path


def _load_env_file() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return (
        f"host={os.environ.get('PGHOST', 'localhost')} "
        f"port={os.environ.get('PGPORT', '5433')} "
        f"dbname={os.environ.get('PGDATABASE', 'postgres')} "
        f"user={os.environ.get('PGUSER', 'postgres')} "
        f"password={os.environ.get('PGPASSWORD', 'rekill')}"
    )


_load_env_file()
DSN = _dsn()

WORLD_SEED = 20261017

# значения по умолчанию для панели управления
DEFAULT_PARAMS = {
    "oos_level": 4,          # 1..10 частота OOS
    "promo_level": 4,        # 1..10 частота распродаж
    "lead_level": 4,         # 1..10 вариативность поставок
    "short_level": 4,        # 1..10 недопоставка
    "rotation_per_month": 10,
    "novelty_per_month": 10,
    "user_suppliers": "",    # id поставщиков в режиме user через запятую
}


def scale(level: int, at_one: float, at_ten: float) -> float:
    """Нелинейный перевод шкалы 1..10 в величину: при 1 даёт at_one, при 10 — at_ten."""
    level = min(10, max(1, int(level)))
    t = ((level - 1) / 9) ** 1.4
    return at_one + (at_ten - at_one) * t


def oos_cycle_prob(level: int) -> float:
    """Доля SKU-циклов, в которых страховой запас обнуляется. При 10 ~ 0.18."""
    return scale(level, 0.005, 0.18)


def promo_monthly_prob(level: int) -> float:
    """Вероятность начала распродажи по SKU за месяц. При 10 ~ 0.12."""
    return scale(level, 0.004, 0.12)


def lead_sigma_factor(level: int) -> float:
    """Множитель разброса срока поставки. При 10 разброс удваивается."""
    return scale(level, 0.6, 2.0)


def short_prob_factor(level: int) -> float:
    """Множитель вероятности недопоставки. При 10 ~ 8% строк."""
    return scale(level, 0.3, 3.0)
