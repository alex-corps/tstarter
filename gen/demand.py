"""Скрытая модель спроса.

Спрос SKU за день складывается из спроса его клиентов. На каждого клиента
накладываются: сезонность категории, годовой тренд, дрейф жизненного цикла,
структурные шоки, распродажи и каннибализация аналогов.
"""

import datetime as dt

import numpy as np

from gen.config import promo_monthly_prob

EPOCH = dt.date(2024, 1, 1)
WINTER_MONTHS = {1, 2, 11, 12}


class DemandModel:
    def __init__(self, world, promo_level: int, seed: int = 1):
        self.world = world
        self.promo_level = promo_level
        self.rng = np.random.default_rng(seed)
        # sku_id -> (дата начала, длительность дней, скидка)
        self.promos: dict[int, tuple] = {}
        # sku_id -> (множитель шока, дата окончания)
        self.shocks: dict[int, tuple] = {}
        # sku_id -> действующая доля в группе аналогов (меняется при ротации)
        self.shares: dict[int, float] = dict(world.cross_share)

    def season_factor(self, root_category_id: int, day: dt.date) -> float:
        shift, winter = self.world.anomalies.get(day.year, (0, 1.0))
        month = (day.month - 1 - shift) % 12
        factor = self.world.season[root_category_id][month]
        if day.month in WINTER_MONTHS:
            factor *= winter
        # плавная интерполяция к соседнему месяцу по дню внутри месяца
        return float(factor)

    def trend_factor(self, day: dt.date) -> float:
        years = (day - EPOCH).days / 365.25
        return 1.0 + 0.10 * years

    def _promo_active(self, sku_id: int, day: dt.date):
        promo = self.promos.get(sku_id)
        if promo and (day - promo[0]).days < promo[1]:
            return promo[2]
        return None

    def maybe_start_promo(self, sku_id: int, day: dt.date, phasing_out: bool) -> None:
        """С вероятностью, зависящей от параметра панели, запускает распродажу."""
        if self._promo_active(sku_id, day) is not None:
            return
        daily_prob = 1 - (1 - promo_monthly_prob(self.promo_level)) ** (1 / 30)
        if phasing_out:
            daily_prob *= 4
        if self.rng.random() < daily_prob:
            duration = int(self.rng.integers(3, 15))
            discount = float(self.rng.uniform(0.10, 0.40))
            self.promos[sku_id] = (day, duration, round(discount, 2))

    def _shock_factor(self, sku_id: int, day: dt.date, shock_prob: float) -> float:
        shock = self.shocks.get(sku_id)
        if shock and day <= shock[1]:
            return shock[0]
        if self.rng.random() < shock_prob / 30:
            factor = float(self.rng.choice([1.8, 2.5, 0.4], p=[0.5, 0.25, 0.25]))
            self.shocks[sku_id] = (factor, day + dt.timedelta(days=int(self.rng.integers(14, 43))))
            return factor
        return 1.0

    def sku_lambda(self, sku_id: int, day: dt.date, available_share: float) -> float:
        """Ожидаемый суммарный спрос SKU за день."""
        root, _, base_daily, drift_rate, _, shock_prob, elasticity, _ = \
            self.world.profile_info[sku_id]
        season = self.season_factor(root, day)
        trend = self.trend_factor(day)
        years = (day - EPOCH).days / 365.25
        drift = float(np.exp(drift_rate * years))
        shock = self._shock_factor(sku_id, day, shock_prob)

        discount = self._promo_active(sku_id, day)
        promo_mult = 1.0
        if discount is not None:
            promo_mult = 1 + elasticity * discount

        share = self.shares.get(sku_id, 1.0) * available_share
        return base_daily * season * trend * drift * shock * promo_mult * share

    def order_batch(self, sku_id: int, lam: float) -> int:
        """Спрос за день с учётом ритма клиентов.

        Редкий спрос приходит пачками: большинство дней нулевые, а в день
        заказа объём большой. Это и даёт lumpy-спрос у оптовиков.
        """
        if lam <= 0:
            return 0
        klass = self.world.profile_info[sku_id][1]
        if klass == "fast":
            return int(self.rng.poisson(lam))
        gap = 20.0 if klass == "lumpy" else 8.0
        batch = lam * gap
        if self.rng.random() > min(0.9, 1.0 / gap):
            return 0
        size = max(1.0, batch)
        # lumpy: редкие и очень неровные по размеру пачки
        shape = 0.6 if klass == "lumpy" else 3.0
        return int(self.rng.gamma(shape=shape, scale=size / shape))

    def split_by_customers(self, sku_id: int, total: int) -> dict[int, int]:
        """Раскладывает спрос SKU по его клиентам пропорционально весам."""
        if total <= 0:
            return {}
        buyer_ids, weights = self.world.sku_customers[sku_id]
        probs = weights / weights.sum()
        if total == 1 or len(buyer_ids) == 1:
            pick = int(self.rng.choice(buyer_ids, p=probs))
            return {pick: total}
        counts = self.rng.multinomial(total, probs)
        return {int(i): int(q) for i, q in zip(buyer_ids, counts) if q > 0}

    def set_share(self, sku_id: int, share: float) -> None:
        self.shares[sku_id] = share

    def active_discount(self, sku_id: int, day: dt.date):
        return self._promo_active(sku_id, day)
