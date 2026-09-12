"""城际转移领域模型：交通方式、方案与建议规则。

这里承载两条设计决定：

1. **交通方式建议由规则给出，不由 LLM 猜**（Q33）。判断依据是客观的——
   距离、高铁耗时、有没有铁路——没有一样需要语言模型。
2. **票价分真价与参考价**（Q14）。12306 能给的存真价，给不了的明确标注为
   参考价，绝不把估价说成实价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# 高铁在这个时长以内，优先推荐铁路
RAIL_PREFERRED_MAX_MINUTES = 240
# 超过这个时长，或没有铁路可选时，优先考虑航空
AIR_PREFERRED_MIN_MINUTES = 360
# 这个距离以内不必坐飞机
SHORT_HAUL_KM = 60.0


class TransferMode(StrEnum):
    """城际转移的交通方式。"""

    RAIL = "rail"
    AIR = "air"
    COACH = "coach"
    DRIVE = "drive"
    OTHER = "other"


class PriceSource(StrEnum):
    """票价的来源。与 `intercity_transfer.price_source` 的取值一一对应。"""

    RAIL_12306 = "12306"
    ESTIMATE = "estimate"
    MANUAL = "manual"


class TrainClass(StrEnum):
    """列车等级。由车次的首字母决定，不靠猜。"""

    HIGH_SPEED = "high_speed"  # G / C，高铁与城际
    BULLET = "bullet"  # D，动车
    CONVENTIONAL = "conventional"  # Z / T / K 等普速

    @classmethod
    def of(cls, service_no: str) -> TrainClass:
        head = (service_no or "").strip()[:1].upper()
        if head in ("G", "C"):
            return cls.HIGH_SPEED
        if head == "D":
            return cls.BULLET
        return cls.CONVENTIONAL

    @property
    def label(self) -> str:
        return {
            TrainClass.HIGH_SPEED: "高铁",
            TrainClass.BULLET: "动车",
            TrainClass.CONVENTIONAL: "普速",
        }[self]


@dataclass(frozen=True)
class TransferOption:
    """一个可选的城际转移方案。

    票价与它的来源必须成对出现：`price` 为 None 时 `price_source` 无意义，
    `price_source` 为估价时 `is_reference_price` 必须为真。
    """

    mode: TransferMode
    service_no: str
    from_station: str
    to_station: str
    dep_time: str
    arr_time: str
    duration_min: int
    price: float | None = None
    price_source: PriceSource = PriceSource.ESTIMATE
    is_reference_price: bool = True
    has_tickets: bool = False
    # 已验证席别的余票状态，如 {"二等座": "有"}。无法确定席别名的一律不写入。
    seats: dict[str, str] = field(default_factory=dict)
    note: str | None = None

    def __post_init__(self) -> None:
        if self.duration_min < 0:
            raise ValueError("耗时不能为负")
        if self.price is not None and self.price < 0:
            raise ValueError("票价不能为负")
        if self.price_source is PriceSource.RAIL_12306 and self.is_reference_price:
            raise ValueError("12306 的实价不应标注为参考价")
        if self.price_source is PriceSource.ESTIMATE and not self.is_reference_price:
            raise ValueError("估价必须标注为参考价")

    @property
    def train_class(self) -> TrainClass:
        return TrainClass.of(self.service_no)


@dataclass(frozen=True)
class TransferAdvice:
    """交通方式建议。"""

    mode: TransferMode
    reason: str
    alternatives: tuple[TransferMode, ...] = ()

    @property
    def mode_label(self) -> str:
        return {
            TransferMode.RAIL: "高铁",
            TransferMode.AIR: "飞机",
            TransferMode.COACH: "大巴",
            TransferMode.DRIVE: "自驾",
            TransferMode.OTHER: "待定",
        }[self.mode]


def format_minutes(minutes: int) -> str:
    """把分钟数写成「4 小时 38 分」这种人话。"""
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} 小时 {rest} 分"
    if hours:
        return f"{hours} 小时"
    return f"{rest} 分"


def advise_mode(
    *,
    best_rail_minutes: int | None,
    rail_available: bool,
    distance_km: float | None = None,
) -> TransferAdvice:
    """按距离与耗时给出交通方式建议。

    规则依据都是客观的：高铁耗时来自 12306 的实际车次，距离来自两座城市的坐标。
    LLM 在旅游交通上的判断往往过时且不可追溯，而这里每一条结论都能指出依据。
    """
    if distance_km is not None and distance_km <= SHORT_HAUL_KM:
        return TransferAdvice(
            mode=TransferMode.COACH,
            reason=f"两地相距约 {distance_km:.0f} 公里，汽车比铁路与航空都省事",
            alternatives=(TransferMode.DRIVE, TransferMode.RAIL),
        )

    if not rail_available or best_rail_minutes is None:
        return TransferAdvice(
            mode=TransferMode.AIR,
            reason="两地之间没有查到可用的铁路车次，建议走航空",
            alternatives=(TransferMode.COACH, TransferMode.DRIVE),
        )

    duration = format_minutes(best_rail_minutes)

    if best_rail_minutes <= RAIL_PREFERRED_MAX_MINUTES:
        return TransferAdvice(
            mode=TransferMode.RAIL,
            reason=f"最快高铁约 {duration}，铁路比航空省去往返机场的时间",
            alternatives=(TransferMode.AIR,),
        )

    if best_rail_minutes < AIR_PREFERRED_MIN_MINUTES:
        return TransferAdvice(
            mode=TransferMode.RAIL,
            reason=f"最快高铁约 {duration}，仍可接受；若赶时间可考虑航空",
            alternatives=(TransferMode.AIR,),
        )

    return TransferAdvice(
        mode=TransferMode.AIR,
        reason=f"最快高铁也要 {duration}，航空更划算",
        alternatives=(TransferMode.RAIL,),
    )


# 各等级列车的每公里估价。用于 12306 拿不到票价时的降级。
# 这些是量级正确的经验值，不是实价——调用方必须标注为参考价。
_RAIL_RATE_PER_KM: dict[TrainClass, float] = {
    TrainClass.HIGH_SPEED: 0.45,
    TrainClass.BULLET: 0.31,
    TrainClass.CONVENTIONAL: 0.16,
}
# 短途有起步价，否则 20 公里的高铁会估出 9 元这种不可能的价
_RAIL_MIN_FARE = 15.0


def estimate_rail_fare(distance_km: float, train_class: TrainClass) -> float:
    """按里程估价。返回的是参考价，调用方必须如实标注。

    12306 的余票响应里没有票价字段，而票价接口当前不可用（实测所有参数变体
    都返回「系统忙」）。所以票价只能估——但估价这件事必须写在数据里，
    不能让用户以为看到的是实价。
    """
    if distance_km <= 0:
        return _RAIL_MIN_FARE
    fare = distance_km * _RAIL_RATE_PER_KM[train_class]
    fare = max(fare, _RAIL_MIN_FARE)
    # 真实票价都是五角结尾，取整到 0.5 元更像那么回事，也避免假精度
    return round(fare * 2) / 2
