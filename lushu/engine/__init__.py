"""引擎适配层。

**这是全项目唯一允许 import third_party 的地方。**
`api`、`services`、`domain`、`adapters`、`store` 一律不得直接引用它，
由 `tests/test_architecture_boundaries.py` 强制。

它的存在是为了让 ADR-0005 的承诺可兑现：一旦需要替换或重写 vendored 引擎，
改动范围就是这个目录，而不是散落全项目的引用点。
"""

from .amap import AmapError, CityMatch, lookup_city, resolve_city
from .bootstrap import engine_info, prepare_engine, reset_engine
from .planning import PlanOutcome, PlanStage, run_plan, stream_plan

__all__ = [
    "AmapError",
    "CityMatch",
    "PlanOutcome",
    "PlanStage",
    "engine_info",
    "lookup_city",
    "prepare_engine",
    "reset_engine",
    "resolve_city",
    "run_plan",
    "stream_plan",
]
