from typing import List

from data.models import ItemSnapshot, PriceRecord


def is_price_stable(price_history: List[PriceRecord], threshold: float = 0.05) -> bool:
    if not price_history:
        return False
    prices = [r.price for r in price_history]
    avg_price = sum(prices) / len(prices)
    if avg_price == 0:
        return False
    # 只有单条记录时无法计算波动，默认视为稳定
    if len(prices) < 2:
        return True
    volatility = (max(prices) - min(prices)) / avg_price
    return volatility <= threshold


def apply_initial_filters(items: List[ItemSnapshot],
                          min_price: float = 20.0,
                          min_volume: int = 100) -> List[ItemSnapshot]:
    """应用BUFF初步筛选条件（兜底校验，主要筛选已在抓取时完成）。"""
    result = []
    for item in items:
        if item.volume < min_volume:
            continue
        if item.buff_price <= min_price:
            continue
        result.append(item)
    return result
