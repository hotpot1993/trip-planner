"""预约规则的种子库、复核与体检。

这个模块管的是本项目**唯一会直接伤害用户**的那份数据：预约规则错一个字段，
用户就会白跑一趟（设计第 6.1 节）。所以它的形状与 M3 的数据管线刻意不同：

- **种子是一份进版本库的文件**（`lushu/seed/booking_rules.json`），不是从网上
  抓来的。预约规则变化慢、数量少（首期 20–30 个），值得人工维护；
  抓来的东西没人签字，错了也追溯不到是谁定的。
- **每条规则都要有来源链接**，且必须复核过（`status='reviewed'`）才对用户可见。
  草案状态是硬门禁（Q10），不是「待办」的另一种说法。
- **种子里的景点名同样要过实体对齐**（复用 M3 的那套），否则规则挂不到任何
  POI 上、也就挂不到任何一天上。对齐不上的条目会被报出来，不静默丢弃。

对应地，`lushu/domain/booking.py` 里那份规则体检是纯函数，所以同一套检查
既跑在库里，也跑在种子文件上——种子还没入库时就该被查一遍。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from lushu.domain.align import AlignOutcome, Mention, align
from lushu.domain.booking import BookingAlert as BookingAlertModel
from lushu.domain.booking import BookingRule as BookingRuleModel
from lushu.domain.booking import (
    Channel,
    ChannelKind,
    ClosedDays,
    Finding,
    PlannedVisit,
    RuleStatus,
    Severity,
    build_alert_list,
    lint_rule,
    lint_rules,
)
from lushu.domain.poi import CandidatePoi
from lushu.services import knowledge_store as ks
from lushu.store.connection import connect

# 种子文件默认位置。跟着包一起走，这样 `pip install` 之后也在
DEFAULT_SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "booking_rules.json"


class SeedError(ValueError):
    """种子文件有问题。消息是给维护种子的人看的中文。"""


# ─── 种子文件的形状 ─────────────────────────────────────────────


@dataclass(frozen=True)
class SeedEntry:
    """种子文件里的一条。**规则内容 + 它挂到哪个景点**。"""

    name: str  # 官方名，给人看
    city: str  # 城市名，高德搜索时限定范围
    booking_required: bool
    # 高德那边的叫法。**官方名与高德名经常不是一回事**，实测：
    #   湖南博物院 → 湖南省博物馆（2022 年改名，高德索引还是旧名）
    #   上海博物馆人民广场馆 → 上海博物馆(人民广场馆)（半角括号）
    #   苏州博物馆（本馆） → 苏州博物馆(本馆)
    # 名称对不上时对齐只能靠字符重合度，而那道门槛是故意设高的
    # （宁可让人来判，也不自动接受一个「看起来挺像」的候选）。
    # 不填就用 name——对得上的那些不需要这一项。
    amap_name: str | None = None
    advance_days: int | None = None
    release_time: str | None = None
    channels: tuple[Channel, ...] = ()
    requires_real_name: bool | None = None
    id_required_note: str | None = None
    closed_days_weekdays: tuple[int, ...] = ()
    evidence_url: str | None = None
    source_kind: str | None = None  # official | authority | secondary
    confidence: str | None = None  # high | medium | low
    releases_at_note: str | None = None
    note: str | None = None

    @property
    def search_name(self) -> str:
        """拿去搜高德的那个名字。"""
        return self.amap_name or self.name

    def as_rule(self, poi_id: str) -> BookingRuleModel:
        """还没入库的规则：一律从草案状态起步，复核是人的动作。"""
        return BookingRuleModel(
            poi_id=poi_id,
            booking_required=self.booking_required,
            status=RuleStatus.DRAFT,
            advance_days=self.advance_days,
            release_time=self.release_time,
            channels=self.channels,
            requires_real_name=self.requires_real_name,
            id_required_note=self.id_required_note,
            closed_days=ClosedDays(weekdays=self.closed_days_weekdays),
            evidence_url=self.evidence_url,
            reviewer_note=self.note,
        )


_KINDS = {item.value for item in ChannelKind}
_SOURCE_KINDS = {"official", "authority", "secondary"}
_CONFIDENCE = {"high", "medium", "low"}


def load_seed(path: Path | str | None = None) -> list[SeedEntry]:
    """读种子文件。

    解析失败一律报错而不是跳过：种子库是**人工维护**的一份短名单，
    一条读不出来的条目意味着有人改坏了它，静默跳过会让规则悄悄少一条。
    """
    target = Path(path) if path is not None else DEFAULT_SEED_PATH
    if not target.is_file():
        raise SeedError(f"找不到种子文件：{target}")

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeedError(f"种子文件不是合法 JSON：{exc}") from exc

    raw_items = payload.get("rules") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        raise SeedError("种子文件应当是一个数组，或带 rules 数组的对象")

    return [_entry(item, index) for index, item in enumerate(raw_items, 1)]


def _entry(raw: object, index: int) -> SeedEntry:
    if not isinstance(raw, dict):
        raise SeedError(f"第 {index} 条不是对象")

    def text(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise SeedError(f"第 {index} 条的 {key} 应当是字符串")
        value = value.strip()
        return value or None

    name = text("name")
    city = text("city")
    if not name or not city:
        raise SeedError(f"第 {index} 条缺 name 或 city")

    booking_required = raw.get("booking_required")
    if not isinstance(booking_required, bool):
        raise SeedError(f"第 {index} 条（{name}）的 booking_required 应当是布尔值")

    advance = raw.get("advance_days")
    if advance is not None and not isinstance(advance, int):
        raise SeedError(f"第 {index} 条（{name}）的 advance_days 应当是整数或 null")

    weekdays = raw.get("closed_days_weekdays") or []
    if not isinstance(weekdays, list) or any(
        not isinstance(day, int) or not 0 <= day <= 6 for day in weekdays
    ):
        raise SeedError(f"第 {index} 条（{name}）的 closed_days_weekdays 应当是 0–6 的整数数组")

    channels_raw = raw.get("channels") or []
    if not isinstance(channels_raw, list):
        raise SeedError(f"第 {index} 条（{name}）的 channels 应当是数组")
    channels: list[Channel] = []
    for channel in channels_raw:
        if not isinstance(channel, dict):
            raise SeedError(f"第 {index} 条（{name}）的渠道不是对象")
        kind = str(channel.get("kind") or "").strip()
        if kind not in _KINDS:
            raise SeedError(
                f"第 {index} 条（{name}）的渠道类型「{kind}」不认识，"
                f"可用的是 {'、'.join(sorted(_KINDS))}"
            )
        channels.append(
            Channel(
                name=str(channel.get("name") or "").strip(),
                kind=ChannelKind(kind),
                url=(str(channel["url"]).strip() if channel.get("url") else None),
            )
        )

    source_kind = text("source_kind")
    if source_kind is not None and source_kind not in _SOURCE_KINDS:
        raise SeedError(f"第 {index} 条（{name}）的 source_kind 只能是 {'、'.join(sorted(_SOURCE_KINDS))}")
    confidence = text("confidence")
    if confidence is not None and confidence not in _CONFIDENCE:
        raise SeedError(f"第 {index} 条（{name}）的 confidence 只能是 {'、'.join(sorted(_CONFIDENCE))}")

    return SeedEntry(
        name=name,
        city=city,
        amap_name=text("amap_name"),
        booking_required=booking_required,
        advance_days=advance,
        release_time=text("release_time"),
        channels=tuple(channels),
        requires_real_name=raw.get("requires_real_name")
        if isinstance(raw.get("requires_real_name"), bool)
        else None,
        id_required_note=text("id_required_note"),
        closed_days_weekdays=tuple(sorted(set(weekdays))),
        evidence_url=text("evidence_url"),
        source_kind=source_kind,
        confidence=confidence,
        releases_at_note=text("releases_at_note"),
        note=text("note"),
    )


def lint_seed(entries: list[SeedEntry], *, today: date) -> list[Finding]:
    """体检种子文件本身，不需要数据库、不需要网络。

    种子里的条目还没挂到 POI 上，所以 `poi_id` 位置先放景点名——
    报出来的问题得让人能一眼找到是哪一个景点。
    """
    rules = [entry.as_rule(poi_id=entry.name) for entry in entries]
    names = {entry.name: f"{entry.name}（{entry.city}）" for entry in entries}
    return lint_rules(rules, today=today, names=names)


# ─── 入库 ────────────────────────────────────────────────────────


@dataclass
class SeedReport:
    """一次种子入库的结果。失败的要能点名，不能只报一个数字。"""

    total: int = 0
    written: int = 0
    aligned: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (景点名, 原因)
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.ERROR]


def resolve_poi(
    entry: SeedEntry,
    *,
    search,
    fetch,
    conn: sqlite3.Connection,
    city_adcode: str | None,
) -> tuple[CandidatePoi | None, str]:
    """把种子里的景点名对到高德实体上，返回（实体，说明）。

    复用 M3 的对齐（`complete_lineage` + `align`）。城市线索由调用方给：
    它必须是**库里已有的**城市 adcode，否则 `align` 的城市门禁会一律拒绝
    （`CandidatePoi.in_city` 对没有 adcode 的候选返回 False）。
    实测 17 条种子里有 11 条卡在这一点上——种子覆盖的城市大多还没进过库。
    """
    from lushu.adapters.poi_lineage import complete_lineage

    found = search(entry.search_name, entry.city)
    if not found.candidates:
        return None, f"高德没搜到「{entry.search_name}」"

    roots = complete_lineage(found.candidates, fetch_ancestor=fetch)
    result = align(
        Mention(name=entry.search_name, city_name=entry.city, city_adcode=city_adcode),
        found.candidates,
        lineage=roots,
    )
    if result.outcome is not AlignOutcome.ALIGNED or result.resolved is None:
        return None, f"对不上实体：{result.reason or result.outcome.value}"
    return result.resolved, result.reason or "名字命中"


def ensure_city(*, name: str, lookup, conn: sqlite3.Connection) -> str | None:
    """把城市名落进 `city` 表，返回它的 adcode。

    已经在库里就直接用——库里那份可能是用户在行程里自己叫的名字，
    不该被种子的写法覆盖。不在库里就走高德解析（城市与 POI 一样以高德为准，
    ADR-0002），用的是行程创建时的同一个入口。
    """
    row = conn.execute("SELECT adcode FROM city WHERE name = ? LIMIT 1", (name,)).fetchone()
    if row is not None:
        return row["adcode"]

    match = lookup(name)
    if match is None or not getattr(match, "adcode", None):
        return None

    from lushu.services.trip_store import CityRef, upsert_cities

    upsert_cities(
        [
            CityRef(
                adcode=match.adcode,
                name=getattr(match, "name", None) or name,
                lat_gcj02=getattr(match, "lat_gcj02", None),
                lng_gcj02=getattr(match, "lng_gcj02", None),
            )
        ],
        conn=conn,
    )
    return match.adcode


def seed_rules(
    *,
    entries: list[SeedEntry] | None = None,
    path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
    search=None,
    fetch=None,
    resolve_city=None,
    today: date | None = None,
    only: list[str] | None = None,
) -> SeedReport:
    """把种子文件写进库里。

    写进去的规则一律是**草案状态**：入库不等于复核。种子文件里的字段只是
    「某人查到的材料」，签字画押是 `review_rule` 那一步的事。
    """
    from lushu.adapters.poi import fetch_poi, search_pois
    from lushu.engine import resolve_city as engine_resolve_city

    items = entries if entries is not None else load_seed(path)
    if only:
        wanted = set(only)
        items = [item for item in items if item.name in wanted]

    owned = conn is None
    active = conn or connect()
    report = SeedReport(total=len(items))
    today = today or date.today()
    searcher = search or (lambda keywords, city: search_pois(keywords, city=city))
    fetcher = fetch or (lambda poi_id: fetch_poi(poi_id))
    city_lookup = resolve_city or engine_resolve_city

    try:
        report.findings = lint_seed(items, today=today)
        # 城市解析每个名字只做一次：17 条种子散在 12 座城市里，
        # 每条都问一次高德是白花钱
        cities: dict[str, str] = {}
        for entry in items:
            if entry.city not in cities:
                cities[entry.city] = (
                    ensure_city(name=entry.city, lookup=city_lookup, conn=active) or ""
                )
            city_adcode = cities[entry.city]
            if not city_adcode:
                report.failed.append((entry.name, f"城市「{entry.city}」在高德查不到"))
                continue

            poi, why = resolve_poi(
                entry,
                search=searcher,
                fetch=fetcher,
                conn=active,
                city_adcode=city_adcode,
            )
            if poi is None:
                report.failed.append((entry.name, why))
                continue
            report.aligned += 1
            ks.save_candidate_poi(conn=active, poi=poi, city_adcode=city_adcode, now=_now())
            write_rule(conn=active, rule=entry.as_rule(poi.poi_id), now=_now())
            report.written += 1
        active.commit()
    finally:
        if owned:
            active.close()
    return report


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def write_rule(*, conn: sqlite3.Connection, rule: BookingRuleModel, now: str) -> None:
    """写一条规则。

    草案与已复核都从这里走——差别只在 `status` 与 `reviewed_at`，
    不再开第二个写入口，免得两处的字段处理慢慢分叉。
    """
    reviewed = rule.status is RuleStatus.REVIEWED
    if reviewed and rule.reviewed_at is None:
        raise ValueError("已复核的规则必须记复核日期，否则算不出复验到期")
    conn.execute(
        "INSERT INTO booking_rule (poi_id, booking_required, advance_days, release_time, "
        "channels_json, requires_real_name, id_required_note, closed_days_json, status, "
        "evidence_url, reviewed_at, reviewer_note, verify_due_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(poi_id) DO UPDATE SET "
        "  booking_required = excluded.booking_required, "
        "  advance_days = excluded.advance_days, "
        "  release_time = excluded.release_time, "
        "  channels_json = excluded.channels_json, "
        "  requires_real_name = excluded.requires_real_name, "
        "  id_required_note = excluded.id_required_note, "
        "  closed_days_json = excluded.closed_days_json, "
        "  status = excluded.status, "
        "  evidence_url = excluded.evidence_url, "
        "  reviewed_at = excluded.reviewed_at, "
        "  reviewer_note = excluded.reviewer_note, "
        "  verify_due_at = excluded.verify_due_at, "
        "  updated_at = excluded.updated_at",
        (
            rule.poi_id,
            1 if rule.booking_required else 0,
            rule.advance_days,
            rule.release_time,
            json.dumps(
                [
                    {"name": item.name, "kind": item.kind.value, "url": item.url}
                    for item in rule.channels
                ],
                ensure_ascii=False,
            ),
            None if rule.requires_real_name is None else int(rule.requires_real_name),
            rule.id_required_note,
            json.dumps(
                {
                    "weekdays": list(rule.closed_days.weekdays),
                    "ranges": [[start.isoformat(), end.isoformat()] for start, end in rule.closed_days.ranges],
                },
                ensure_ascii=False,
            ),
            rule.status.value,
            rule.evidence_url,
            rule.reviewed_at.isoformat() if rule.reviewed_at else None,
            rule.reviewer_note,
            rule.verify_due_at.isoformat() if rule.verify_due_at else None,
            now,
        ),
    )


def review_rule(
    *,
    conn: sqlite3.Connection,
    poi_id: str,
    reviewed_at: date | None = None,
    evidence_url: str | None = None,
    note: str | None = None,
) -> bool:
    """复核一条规则，让它对用户可见。

    体检不通过的**一律不许复核**——这是这道门禁唯一的意义所在。
    与其把一条算不出放票日的规则放出去，不如让它继续待在草案里报错。
    """
    rule = get_rule(poi_id, conn=conn)
    if rule is None:
        return False
    today = reviewed_at or date.today()
    candidate = BookingRuleModel(
        poi_id=rule.poi_id,
        booking_required=rule.booking_required,
        status=RuleStatus.REVIEWED,
        advance_days=rule.advance_days,
        release_time=rule.release_time,
        channels=rule.channels,
        requires_real_name=rule.requires_real_name,
        id_required_note=rule.id_required_note,
        closed_days=rule.closed_days,
        evidence_url=evidence_url or rule.evidence_url,
        reviewed_at=today,
        reviewer_note=note or rule.reviewer_note,
    )
    blocking = [
        item
        for item in lint_rule(candidate, today=today)
        if item.severity is Severity.ERROR
    ]
    if blocking:
        return False

    write_rule(conn=conn, rule=candidate, now=_now())
    return True


# ─── 读 ──────────────────────────────────────────────────────────


def _rule_from_row(row: sqlite3.Row) -> BookingRuleModel:
    channels = ks.dict_items(row["channels_json"])
    closed = json.loads(row["closed_days_json"] or "{}") or {}
    ranges: list[tuple[date, date]] = []
    for pair in closed.get("ranges") or []:
        if isinstance(pair, list) and len(pair) == 2:
            ranges.append((date.fromisoformat(pair[0]), date.fromisoformat(pair[1])))

    return BookingRuleModel(
        poi_id=row["poi_id"],
        booking_required=bool(row["booking_required"]),
        status=RuleStatus(row["status"]),
        advance_days=row["advance_days"],
        release_time=row["release_time"],
        channels=tuple(
            Channel(
                name=str(item.get("name") or ""),
                kind=ChannelKind(str(item.get("kind") or "web")),
                url=item.get("url"),
            )
            for item in channels
        ),
        requires_real_name=None if row["requires_real_name"] is None else bool(row["requires_real_name"]),
        id_required_note=row["id_required_note"],
        closed_days=ClosedDays(weekdays=tuple(closed.get("weekdays") or ()), ranges=tuple(ranges)),
        evidence_url=row["evidence_url"],
        reviewed_at=date.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None,
        reviewer_note=row["reviewer_note"],
    )


def get_rule(poi_id: str, *, conn: sqlite3.Connection | None = None) -> BookingRuleModel | None:
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT * FROM booking_rule WHERE poi_id = ?", (poi_id,)
        ).fetchone()
    finally:
        if owned:
            active.close()
    return _rule_from_row(row) if row is not None else None


def all_rules(*, conn: sqlite3.Connection | None = None) -> list[BookingRuleModel]:
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute("SELECT * FROM booking_rule ORDER BY poi_id").fetchall()
    finally:
        if owned:
            active.close()
    return [_rule_from_row(row) for row in rows]


def poi_names(*, conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """poi_id → 名字。报告里要印名字，只有 id 的报告没人看得下去。"""
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute("SELECT amap_poi_id, name FROM poi").fetchall()
    finally:
        if owned:
            active.close()
    return {row["amap_poi_id"]: row["name"] for row in rows}


def lint_database(
    *, conn: sqlite3.Connection | None = None, today: date | None = None
) -> list[Finding]:
    """体检库里的全部规则。"""
    owned = conn is None
    active = conn or connect()
    try:
        return lint_rules(
            all_rules(conn=active),
            today=today or date.today(),
            names=poi_names(conn=active),
        )
    finally:
        if owned:
            active.close()


def rule_stats(*, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """规则库的规模。验收条件「20 至 30 个景点的规则已复核并可用」看的就是这里。"""
    owned = conn is None
    active = conn or connect()
    try:
        row = active.execute(
            "SELECT COUNT(*) AS total, "
            "  SUM(status = 'reviewed') AS reviewed, "
            "  SUM(booking_required) AS required, "
            "  SUM(status = 'reviewed' AND evidence_url IS NOT NULL) AS with_evidence "
            "FROM booking_rule"
        ).fetchone()
    finally:
        if owned:
            active.close()
    return {
        "total": row["total"] or 0,
        "reviewed": row["reviewed"] or 0,
        "required": row["required"] or 0,
        "with_evidence": row["with_evidence"] or 0,
    }


# ─── 预约清单 ────────────────────────────────────────────────────


def planned_visits(
    trip_id: str, *, conn: sqlite3.Connection | None = None
) -> list[PlannedVisit]:
    """行程里安排了的景点。

    只算 `day_item.poi_id` 不为空的天项——没对上实体的景点挂不上规则，
    这正是 M3 的对齐要解决的问题。
    """
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT DISTINCT di.poi_id, COALESCE(p.name, di.title) AS poi_name, d.date, "
            "  cs.city_name "
            "FROM day_item di "
            "JOIN day d ON d.id = di.day_id "
            "JOIN city_stay cs ON cs.id = d.city_stay_id "
            "LEFT JOIN poi p ON p.amap_poi_id = di.poi_id "
            "WHERE d.trip_id = ? AND di.poi_id IS NOT NULL AND di.kind = 'poi' "
            "ORDER BY d.date, di.seq",
            (trip_id,),
        ).fetchall()
    finally:
        if owned:
            active.close()

    return [
        PlannedVisit(
            poi_id=row["poi_id"],
            poi_name=row["poi_name"] or row["poi_id"],
            visit_date=date.fromisoformat(row["date"]),
            city_name=row["city_name"],
        )
        for row in rows
    ]


def alerts_for_trip(
    trip_id: str, *, conn: sqlite3.Connection | None = None, today: date | None = None
) -> list[BookingAlertModel]:
    """某份行程的预约清单。

    `build_alert_list` 会把草案状态的规则静默跳过，所以「这个景点要预约但
    没有提醒」有两种可能：没录规则，或规则还没复核。调用方要看得出区别，
    所以另外给一个 `pending_rule_pois`。
    """
    owned = conn is None
    active = conn or connect()
    try:
        rules = all_rules(conn=active)
        visits = planned_visits(trip_id, conn=active)
    finally:
        if owned:
            active.close()
    return build_alert_list(rules, visits, today or date.today())


def pending_rule_pois(trip_id: str, *, conn: sqlite3.Connection | None = None) -> list[str]:
    """行程里那些**有规则但还没复核**的景点。

    界面要能说清「这里没有提醒，是因为规则还是草案」，而不是让用户以为
    这个景点不用预约。
    """
    owned = conn is None
    active = conn or connect()
    try:
        rows = active.execute(
            "SELECT DISTINCT di.poi_id AS poi_id, COALESCE(p.name, di.title) AS name "
            "FROM day_item di JOIN day d ON d.id = di.day_id "
            "JOIN booking_rule br ON br.poi_id = di.poi_id "
            "LEFT JOIN poi p ON p.amap_poi_id = di.poi_id "
            "WHERE d.trip_id = ? AND br.status = 'draft'",
            (trip_id,),
        ).fetchall()
    finally:
        if owned:
            active.close()
    return [row["name"] or row["poi_id"] for row in rows]
