from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class PriceRecord:
    date: date
    price: float
    volume: int = 0


@dataclass
class ItemSnapshot:
    item_id: str
    name: str
    buff_price: float
    volume: int
    turnover: float = field(init=False)
    price_history: List[PriceRecord] = field(default_factory=list)
    steam_url: Optional[str] = field(default=None)
    steam_price: Optional[float] = field(default=None)
    steam_sold_count: int = 0
    steam_price_history: List[PriceRecord] = field(default_factory=list)

    def __post_init__(self):
        self.turnover = self.buff_price * self.volume
