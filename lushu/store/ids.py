"""标识符生成。

带前缀的短随机串。前缀不是为了好看——是为了在日志、数据库与错误信息里
一眼看出这是哪种对象，省掉一次翻 schema 的时间。
"""

from __future__ import annotations

import uuid

# 各类对象的标识前缀
TRIP = "trip"
CITY_STAY = "cs"
DAY = "day"
DAY_ITEM = "di"
TRANSFER = "tf"
LEG = "leg"
BUDGET = "bud"
WORKBENCH = "wb"


def new_id(prefix: str) -> str:
    """生成一个带前缀的标识符，例如 `trip_3f9a2c81d4e7`。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
