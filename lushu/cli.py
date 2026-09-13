"""命令行入口。命令名 `ls`（路书）。

M0 阶段提供 `serve`、`init-db`、`doctor`。M3 阶段加入数据链路与评测的子命令。

`booking` 与 `verify` 都已就位，这里**不注册空壳**——命令列表应当如实
反映能力，而不是列出一堆点了就报错的入口。计划中的命令写在 `serve`
帮助的末尾备查。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from lushu import __version__, config

if TYPE_CHECKING:  # 只为了标注。命令实现在函数里按需导入，启动代价是零
    from lushu.services.verify import DueItem

PLANNED_PIPELINE = """\
还没做的子命令（M7 起）：
  poi warm                        按城市预热高德 POI
"""

# 导入时提示的站点标识，与 source_document.site 的取值一致
_SITE_CHOICES = ("mafengwo", "zhihu", "qiongyou", "ctrip", "xhs", "manual")


def _tolerate_unprintable(stream) -> None:
    """控制台编不出来的字符降级成 `?`，而不是让整条命令崩掉。

    这条是为**报错路径本身**准备的。契约校验与规则体检发现 ERROR 时会打印 `✗`
    （U+2717），而 Windows 控制台默认是 GBK（cp936），`✗` 不在 GBK 里——于是
    `print` 直接抛 `UnicodeEncodeError`，人看到的是一串 traceback，
    **错误内容一个字都没印出来**。这正是最该看见它的时刻。

    同一个坑上还排着 `✅`、`⚠`，分别在规则列表、金标准进度与评测里。

    放宽的是 `errors` 而不是 `encoding`：中文在 GBK 里有，照常显示；
    编不出来的退化成一个问号。**装饰性符号可以退化，消息不行。**
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return  # 被重定向到了没有 reconfigure 的对象（测试里的捕获、StringIO）
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError):  # 流已关闭或不可重配，不该因此崩
        pass


def main(argv: list[str] | None = None) -> int:
    _tolerate_unprintable(sys.stdout)
    _tolerate_unprintable(sys.stderr)

    parser = argparse.ArgumentParser(
        prog="ls",
        description="路书 —— 本地优先的旅行攻略规划工具",
    )
    parser.add_argument("--version", action="version", version=f"路书 {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动本地服务")
    serve.add_argument("--host", default=config.HOST, help=f"监听地址，默认 {config.HOST}")
    serve.add_argument("--port", type=int, default=config.PORT, help=f"端口，默认 {config.PORT}")
    serve.add_argument("--reload", action="store_true", help="代码变更时自动重启（开发用）")
    serve.epilog = PLANNED_PIPELINE
    serve.formatter_class = argparse.RawDescriptionHelpFormatter

    sub.add_parser("init-db", help="建立或升级表结构")
    sub.add_parser("doctor", help="环境自检")

    _register_ingest(sub)
    _register_extract(sub)
    _register_group(sub)
    _register_align(sub)
    _register_pipeline(sub)
    _register_eval(sub)
    _register_booking(sub)
    _register_verify(sub)
    _register_trip(sub)
    _register_export(sub)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handler = getattr(args, "_handler", None)
    if handler is not None:
        return handler(args)

    if args.command == "serve":
        return _serve(args)
    if args.command == "init-db":
        return _init_db()
    if args.command == "doctor":
        return _doctor()
    return 2


# ─── M3 数据链路 ────────────────────────────────────────────────


def _register_ingest(sub) -> None:
    ingest = sub.add_parser("ingest", help="导入攻略素材")
    actions = ingest.add_subparsers(dest="action")

    paste = actions.add_parser("paste", help="粘贴一篇正文")
    paste.add_argument("--file", type=Path, help="从文件读取正文（不给则读标准输入）")
    paste.add_argument("--title", help="标题")
    paste.add_argument("--url", help="原文网址，用于识别站点")
    paste.add_argument("--author", help="作者")
    paste.add_argument("--site", choices=_SITE_CHOICES, help="站点标识，不给则按网址推断")
    paste.set_defaults(_handler=_cmd_ingest_paste)

    import_cmd = actions.add_parser("import", help="从本地文件批量导入")
    import_cmd.add_argument("paths", nargs="+", type=Path, help="素材文件或目录")
    import_cmd.add_argument("--site", choices=_SITE_CHOICES, default="manual", help="站点标识")
    import_cmd.set_defaults(_handler=_cmd_ingest_import)

    fetch = actions.add_parser("fetch", help="抓取一个公开页面")
    fetch.add_argument("url", help="页面网址。**不碰需要登录的内容**")
    fetch.set_defaults(_handler=_cmd_ingest_fetch)

    ingest.set_defaults(_handler=lambda _args: _usage(ingest))


def _register_extract(sub) -> None:
    extract = sub.add_parser("extract", help="提纯：抽出结构化候选并校验引文")
    actions = extract.add_subparsers(dest="action")

    run = actions.add_parser("run", help="批量提纯（已成功提纯过的会跳过）")
    run.add_argument("--limit", type=int, default=50, help="本次最多处理几篇，默认 50")
    run.add_argument("--document", action="append", dest="documents", help="只处理指定素材 id")
    run.add_argument(
        "--force",
        action="store_true",
        help="连提纯过的也重跑（命中缓存不花钱）。用于补齐评测要用的候选载荷",
    )
    run.set_defaults(_handler=_cmd_extract_run)

    stats = actions.add_parser("stats", help="链路各环节的规模")
    stats.set_defaults(_handler=_cmd_stats)

    extract.set_defaults(_handler=lambda _args: _usage(extract))


def _register_group(sub) -> None:
    group = sub.add_parser("group", help="独立来源归组")
    actions = group.add_subparsers(dest="action")

    run = actions.add_parser("run", help="按结论重合度归组（需先跑过提纯）")
    run.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="判为同源所需的结论重合比例，默认 0.8。调低会把独立来源错并",
    )
    run.set_defaults(_handler=_cmd_group_run)

    group.set_defaults(_handler=lambda _args: _usage(group))


def _register_align(sub) -> None:
    align = sub.add_parser("align", help="实体对齐：提及名到高德 POI")
    actions = align.add_subparsers(dest="action")

    run = actions.add_parser("run", help="处置待对齐队列（对得上的落库成结论）")
    run.add_argument("--limit", type=int, default=50, help="本次最多处理几条，默认 50")
    run.set_defaults(_handler=_cmd_align_run)

    pending = actions.add_parser("list", help="看待对齐队列")
    pending.add_argument("--limit", type=int, default=50)
    pending.set_defaults(_handler=_cmd_align_list)

    show = actions.add_parser("show", help="看某个景点上的结论")
    show.add_argument("poi_id")
    show.set_defaults(_handler=_cmd_align_show)

    audit = actions.add_parser("audit", help="体检：有没有结论挂到了子点上（违反 ADR-0009）")
    audit.set_defaults(_handler=_cmd_align_audit)

    merge = actions.add_parser(
        "merge-pois", help="把同一处地方的两个实体并成一个（ADR-0002 的补救）"
    )
    merge.add_argument("--apply", action="store_true", help="真的合并。不给就只报告")
    merge.set_defaults(_handler=_cmd_align_merge_pois)

    recheck = actions.add_parser(
        "recheck", help="重新对齐：看哪些结论的落点在算法改进后已经过时"
    )
    recheck.add_argument("--apply", action="store_true", help="真的搬。不给就只报告")
    recheck.set_defaults(_handler=_cmd_align_recheck)

    align.set_defaults(_handler=lambda _args: _usage(align))


def _register_pipeline(sub) -> None:
    pipeline = sub.add_parser("pipeline", help="整条链路")
    actions = pipeline.add_subparsers(dest="action")

    run = actions.add_parser("run", help="导入后的全流程：提纯 → 对齐 → 归组 → 合并")
    run.add_argument("--limit", type=int, default=50, help="每步最多处理几篇，默认 50")
    run.set_defaults(_handler=_cmd_pipeline_run)

    pipeline.set_defaults(_handler=lambda _args: _usage(pipeline))


def _register_eval(sub) -> None:
    eval_cmd = sub.add_parser("eval", help="金标准集与评测")
    actions = eval_cmd.add_subparsers(dest="action")

    gold = actions.add_parser("gold", help="看/改金标准集")
    gold.add_argument("--document", help="只看这一篇的标注")
    gold.add_argument("--show", action="store_true", help="显示每篇的标注条目")
    gold.add_argument("--add", metavar="DOC_ID", help="给某篇加一条标注")
    gold.add_argument("--quote", help="标注引用的原文片段，必须原样来自正文")
    gold.add_argument("--polarity", choices=("avoid", "highlight"), help="避坑还是打卡")
    gold.add_argument("--subject", help="提及名，如「故宫」「兵马俑」")
    gold.add_argument("--poi", help="这个提及指向的高德 POI id")
    gold.add_argument("--facet", help="facet，如 queue / entrance / price_diff")
    gold.add_argument("--remove", metavar="LABEL_ID", help="删掉一条标注")
    gold.add_argument("--set-poi", metavar="LABEL_ID", help="给已有标注补上它指向的 POI")
    gold.add_argument("--done", metavar="DOC_ID", help="标记某篇标注完成（可以一条结论都没有）")
    gold.add_argument("--seen-model", action="store_true", help="标记时声明看过模型输出")
    gold.set_defaults(_handler=_cmd_eval_gold)

    run = actions.add_parser("run", help="跑评测，报出三个指标")
    run.add_argument("--document", action="append", dest="documents", help="只评这几篇")
    run.add_argument("--limit", type=int, default=5, help="每篇最多列几条漏抽/多抽，默认 5")
    run.set_defaults(_handler=_cmd_eval_run)

    eval_cmd.set_defaults(_handler=lambda _args: _usage(eval_cmd))


def _register_booking(sub) -> None:
    booking = sub.add_parser("booking", help="预约规则：种子库、复核与体检")
    actions = booking.add_subparsers(dest="action")

    list_cmd = actions.add_parser("list", help="看规则库现状")
    list_cmd.set_defaults(_handler=_cmd_booking_list)

    lint = actions.add_parser("lint", help="体检：哪些规则会误导用户")
    lint.add_argument(
        "--seed", action="store_true", help="体检种子文件本身（不需要网络与数据库）"
    )
    lint.add_argument("--strict", action="store_true", help="有 error 时返回非零")
    lint.set_defaults(_handler=_cmd_booking_lint)

    seed = actions.add_parser("seed", help="把种子文件写进库里（要对齐实体）")
    seed.add_argument("--file", type=Path, help="种子文件路径，默认用包内的那份")
    seed.add_argument("--only", action="append", dest="only", help="只处理指定景点名")
    seed.add_argument("--dry-run", action="store_true", help="只体检种子，不写库、不联网")
    seed.set_defaults(_handler=_cmd_booking_seed)

    review = actions.add_parser("review", help="复核一条规则，让它对用户可见")
    review.add_argument("poi_id", nargs="?", help="要复核的那条规则")
    review.add_argument(
        "--all",
        action="store_true",
        help="把体检没有 error 的草案全部复核掉。批量签字，先跑 ls booking lint 看清楚",
    )
    review.add_argument("--evidence", help="来源链接，不给就用种子里的")
    review.add_argument("--note", help="复核备注")
    review.set_defaults(_handler=_cmd_booking_review)

    ics = actions.add_parser("ics", help="把某份行程的预约提醒导出成 .ics")
    ics.add_argument("trip_id")
    ics.add_argument("--out", type=Path, help="输出文件，不给就打到标准输出")
    ics.add_argument("--today", help="按这一天算紧迫度，格式 YYYY-MM-DD（核对用）")
    ics.set_defaults(_handler=_cmd_booking_ics)

    booking.set_defaults(_handler=lambda _args: _usage(booking))


def _register_verify(sub) -> None:
    """复验扫描（设计 4.6 / Q49）。

    到期是个静默事件：库里那行数据一个字都没变，界面上也看不出异样，
    用户看到的仍然是三个月前核的那个放票时刻。所以要有这么一条命令，
    而且它默认只报不改——改状态是 `--apply`，得有人明确说要改。
    """
    verify = sub.add_parser("verify", help="复验：哪些结论与规则已经该重新核了")
    actions = verify.add_subparsers(dest="action")

    scan = actions.add_parser("scan", help="扫描到期与快到期的东西")
    scan.add_argument(
        "--days",
        type=int,
        default=None,
        help="提前多少天开始提醒，默认 14。调大它回答的是「出发前哪些会到期」",
    )
    scan.add_argument(
        "--apply",
        action="store_true",
        help="把已经过期的结论标成待复验（只改状态，不删、界面上照样展示）",
    )
    scan.add_argument("--today", help="按这一天算，格式 YYYY-MM-DD（核对用）")
    scan.set_defaults(_handler=_cmd_verify_scan)

    verify.set_defaults(_handler=lambda _args: _usage(verify))


def _register_trip(sub) -> None:
    """行程级的数据维护。

    这里放的是「已经生成好的行程，数据还没补齐」这一类活儿——
    它们不影响新行程（新行程走的是修好之后的转换层），只影响历史数据。
    """
    trip = sub.add_parser("trip", help="已有行程的数据维护")
    actions = trip.add_subparsers(dest="action")

    coords = actions.add_parser("coords", help="给缺坐标的餐饮天项补坐标")
    coords.add_argument("trip_id", nargs="?", help="行程 id，不给就处理全部行程")
    coords.add_argument("--apply", action="store_true", help="写进库里；不给就只报算出来的结果")
    coords.add_argument("--pause", type=float, default=None, help="每次搜索之间歇几秒，默认 0.4")
    coords.set_defaults(_handler=_cmd_trip_coords)

    trip.set_defaults(_handler=lambda _args: _usage(trip))


def _cmd_trip_coords(args: argparse.Namespace) -> int:
    """给缺坐标的餐饮天项补坐标。

    默认只报不改。补一条错的坐标比留空更坏：路书的坐标是**唯一的空间线索**
    （没有地图），「步行 800 米」就是拿它算出来的——错了看不出来。
    """
    from lushu.services import meal_coords

    slots = meal_coords.pending_meals(trip_id=args.trip_id)
    scope = args.trip_id or "全部行程"
    print(f"缺坐标的餐饮项：{len(slots)} 个（{scope}）")
    if not slots:
        print()
        print("没有要补的。")
        return 0

    interval = meal_coords.PAUSE_SECONDS if args.pause is None else args.pause
    print(f"逐个到高德查一遍，每次间隔 {interval} 秒（QPS 限制）")
    print()

    from lushu.adapters.poi import search_around_pois, search_pois

    report = meal_coords.plan_fill(
        slots,
        search=lambda keywords, city: search_pois(keywords, city=city),
        around=lambda keywords, lat_gcj02, lng_gcj02: search_around_pois(
            keywords, lat_gcj02=lat_gcj02, lng_gcj02=lng_gcj02
        ),
        pause=interval,
    )
    for fix in report.fixes:
        where = f"{fix.slot.day_date} {fix.slot.city_name}"
        if fix.resolved:
            print(f"  ✅ {where} {fix.slot.title}")
            origin = f"（在{fix.near}周边找到）" if fix.near else ""
            print(f"      → {fix.matched_name}{origin}　{fix.lat_gcj02},{fix.lng_gcj02}")
            if fix.address:
                print(f"      {fix.address}")
        else:
            print(f"  —— {where} {fix.slot.title}")
            print(f"      {fix.reason}")

    print()
    print(f"查到 {len(report.resolved)} 个，留空 {len(report.unresolved)} 个")
    if report.unresolved:
        print("留空的是**故意**的：对不上实体就不写坐标。通用菜名基本都会留空——")
        print("搜索能搜到一家同名小店，但那多半不是行程里说的那一家。")

    if not args.apply:
        if report.resolved:
            print()
            print("还没有写库。加 --apply 把上面查到的坐标写进天项，再重新导出路书。")
        return 0

    changed = meal_coords.apply_fixes(report.fixes)
    print()
    print(f"已写入 {len(changed)} 个天项。重新跑 ls export 让路书里的路段说明补上。")
    return 0


def _register_export(sub) -> None:
    export = sub.add_parser("export", help="把行程导出成单个 HTML 路书")
    export.add_argument("trip_id", nargs="?", help="行程 id，不给就列出可选行程")
    export.add_argument("--out", type=Path, help="输出路径，默认 data/<trip_id>.html")
    export.add_argument("--check", action="store_true", help="只跑契约校验，不写文件")
    export.set_defaults(_handler=_cmd_export)


def _cmd_export(args: argparse.Namespace) -> int:
    """导出单文件 HTML 路书。

    设计第八节说「生成后必须跑一次契约校验，有错误必须修复后重跑」——
    有错误就**不写文件**：一份到了当地打不开的路书比没有更糟，人会以为带上了。
    """
    from lushu.domain.roadbook import errors, validate
    from lushu.services import roadbook_render as render
    from lushu.services import roadbook_service
    from lushu.store import connect

    if not args.trip_id:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, name, start_date FROM trip ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            print("库里还没有行程")
            return 0
        print("导出哪一份？把 id 传给 ls export：")
        for row in rows:
            print(f"  {row['id']}  {row['name']}  {row['start_date']}")
        return 0

    try:
        book, warnings = roadbook_service.assemble(args.trip_id)
    except ValueError as exc:
        print(f"装配失败：{exc}")
        return 1

    problems = validate(book)
    fatal = errors(problems)
    print(f"《{book.name}》{book.total_days} 天、{book.item_count} 个天项、"
          f"{len(book.bookings)} 条预约")

    if warnings:
        print()
        for item in warnings:
            print(f"  ! {item}")

    if problems:
        print()
        print("契约校验：")
        for item in problems:
            print(f"  {item}")

    if fatal:
        print()
        print(f"有 {len(fatal)} 处错误，**没有写出文件**——")
        print("一份到了当地打不开的路书比没有更糟：人会以为带上了。")
        return 1

    if args.check:
        print()
        print("契约校验通过（没有写文件）")
        return 0

    html = render.render(book)
    refs = render.external_resources(html)
    if refs:
        print(f"产物里有 {len(refs)} 处会自动加载的外部资源，不能算离线可读")
        return 1

    target = args.out or (config.EXPORTS_DIR / f"{args.trip_id}.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")

    print()
    print(f"已写出 {target}（{target.stat().st_size / 1024:.0f} KB，"
          f"零外部引用，可离线打开）")
    print("传手机上就能用：微信发给自己、AirDrop、或者直接拷进文件 App。")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"路书 {__version__} 正在启动：http://{args.host}:{args.port}")
    uvicorn.run(
        "lushu.app:app",
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
    )
    return 0


def _init_db() -> int:
    from lushu.engine import prepare_engine
    from lushu.store import initialize

    version = initialize()
    print(f"表结构就绪，版本 {version}")
    print(f"库文件：{config.DB_PATH}")

    info = prepare_engine()
    if info.available:
        print(f"引擎表结构就绪，上游提交 {info.upstream_commit or '未知'}")
    else:
        print(f"引擎不可用：{info.error}")
        return 1
    return 0


def _doctor() -> int:
    """环境自检。只报告，不修改任何东西。"""
    from lushu.engine import engine_info
    from lushu.store import SCHEMA_VERSION, connect

    ok = True

    print(f"路书 {__version__}")
    print(f"Python {sys.version.split()[0]}")
    print()

    print(f"项目根目录    {config.ROOT_DIR}")
    print(f"数据目录      {config.DATA_DIR}")
    print(f"库文件        {config.DB_PATH}")

    env_exists = config.ENV_FILE.is_file()
    env_hint = "已找到" if env_exists else "未找到（复制 .env.example 为 .env.local）"
    print(f"环境文件      {config.ENV_FILE.name} {env_hint}")
    if not env_exists:
        ok = False

    missing = config.missing_required_keys()
    if missing:
        print(f"缺失配置      {'、'.join(missing)}")
        ok = False
    else:
        print("缺失配置      无")

    db_ok = False
    if config.DB_PATH.exists():
        try:
            with connect() as conn:
                actual = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if actual == SCHEMA_VERSION:
                print(f"表结构        已就绪（版本 {actual}）")
                db_ok = True
            else:
                print(f"表结构        版本 {actual}，需要升级到 {SCHEMA_VERSION}（运行 ls init-db）")
        except Exception as exc:
            print(f"表结构        读取失败：{type(exc).__name__}: {exc}")
    else:
        print("表结构        库文件不存在（运行 ls init-db）")
    if not db_ok:
        ok = False

    info = engine_info()
    if info.available:
        print(f"引擎          可用（上游提交 {info.upstream_commit or '未知'}）")
        # 用去掉前缀的完整路径，避免 planning.graph 与 chat.graph 都显示成 graph
        labels = "、".join(m.removeprefix("third_party.floattrip.") for m in info.modules)
        print(f"引擎模块      {len(info.modules)} 个：{labels}")
    else:
        print(f"引擎          不可用：{info.error}")
        ok = False

    dist = config.WEB_DIST_DIR / "index.html"
    print(f"前端产物      {'已构建' if dist.is_file() else '未构建（cd web && pnpm build）'}")

    print()
    print("自检通过。" if ok else "自检发现问题，见上。")
    return 0 if ok else 1


# ─── 预约规则 ────────────────────────────────────────────────────

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _cmd_booking_list(_args: argparse.Namespace) -> int:
    from lushu.services.booking_store import all_rules, poi_names, rule_stats

    stats = rule_stats()
    print(f"规则 {stats['total']} 条｜已复核 {stats['reviewed']} 条"
          f"｜其中需要预约 {stats['required']} 条")
    # 验收条件看的就是这一行
    target = 20
    if stats["reviewed"] >= target:
        print(f"已达 M4 的验收条件（{target} 至 30 个景点的规则已复核并可用）")
    else:
        print(f"M4 的验收条件是 20 至 30 个景点已复核，现在 {stats['reviewed']} 个")
    print()

    rules = all_rules()
    if not rules:
        print("规则库是空的。先跑 ls booking seed 把种子写进来")
        return 0

    names = poi_names()
    for rule in sorted(rules, key=lambda item: (item.status.value, item.poi_id)):
        mark = "✅" if rule.status.value == "reviewed" else "  "
        label = names.get(rule.poi_id, rule.poi_id)
        days = f"提前 {rule.advance_days} 天" if rule.advance_days is not None else "提前天数未填"
        at = f" {rule.release_time}" if rule.release_time else ""
        who = f"· {len(rule.channels)} 个渠道" if rule.channels else "· 没有渠道"
        print(f"  {mark} {label:<22}{days}{at}  {who}")
        if rule.closed_days.weekdays:
            closed = "、".join(_WEEKDAYS[day] for day in rule.closed_days.weekdays)
            print(f"       闭馆：{closed}")
        if rule.status.value == "draft":
            print("       （草案：不对用户可见，ls booking review 之后才生效）")
    return 0


def _cmd_booking_lint(args: argparse.Namespace) -> int:
    from datetime import date

    from lushu.services import booking_store as bs

    if args.seed:
        entries = bs.load_seed()
        findings = bs.lint_seed(entries, today=date.today())
        print(f"体检种子文件：{len(entries)} 条")
    else:
        findings = bs.lint_database()
        print(f"体检库里的规则：{bs.rule_stats()['total']} 条")

    if not findings:
        print()
        print("没有发现问题。")
        return 0

    errors = [item for item in findings if item.severity.value == "error"]
    warnings = [item for item in findings if item.severity.value != "error"]
    print()
    for item in errors + warnings:
        print(f"  {item}")
    print()
    print(f"错误 {len(errors)} 处，提醒 {len(warnings)} 处")
    if errors:
        print()
        print("错误必须修：预约规则错一个字段，用户就会白跑一趟。")
        print("有错误的规则复核不过（ls booking review 会拒绝），也就不会对用户可见。")
    return 1 if (errors and args.strict) else 0


def _cmd_booking_seed(args: argparse.Namespace) -> int:
    from datetime import date

    from lushu.services import booking_store as bs

    try:
        entries = bs.load_seed(args.file)
    except bs.SeedError as exc:
        print(f"种子文件有问题：{exc}")
        return 1

    if args.only:
        wanted = set(args.only)
        entries = [item for item in entries if item.name in wanted]
        if not entries:
            print(f"种子里没有这些景点：{'、'.join(sorted(wanted))}")
            return 1

    if args.dry_run:
        findings = bs.lint_seed(entries, today=date.today())
        print(f"种子 {len(entries)} 条，只体检不写库：")
        for item in findings:
            print(f"  {item}")
        if not findings:
            print("  没有发现问题")
        return 0

    print(f"把 {len(entries)} 条种子写进库里，每条都要先在高德对上实体……")
    report = bs.seed_rules(entries=entries)
    print()
    print(f"对上实体并写入 {report.written} 条（共 {report.total} 条）")
    if report.kept_reviewed:
        print(f"  其中 {report.kept_reviewed} 条内容未变，复核状态保住了——"
              f"加新规则不会让已复核的那些失效")
    if report.failed:
        print(f"没写进去 {len(report.failed)} 条：")
        for name, why in report.failed:
            print(f"  {name}：{why}")
    if report.errors:
        print()
        print(f"种子本身有 {len(report.errors)} 处错误（不影响入库，但复核会被拒）：")
        for item in report.errors:
            print(f"  {item}")
    print()
    print("写进去的都是**草案**状态，不对用户可见。")
    print("复核：ls booking review <poi_id> --evidence <来源链接>")
    return 0 if report.written else 1


def _cmd_booking_review(args: argparse.Namespace) -> int:
    from lushu.services import booking_store as bs
    from lushu.store import transaction

    if args.all:
        return _review_all(args)

    if not args.poi_id:
        print("要复核哪一条？给出 poi_id，或用 --all 批量")
        return 2

    rule = bs.get_rule(args.poi_id)
    if rule is None:
        print(f"没有这条规则：{args.poi_id}")
        return 1

    with transaction() as conn:
        ok = bs.review_rule(
            conn=conn,
            poi_id=args.poi_id,
            evidence_url=args.evidence,
            note=args.note,
        )

    if not ok:
        # 门禁拦下了：把原因说出来，别只说一句「不行」
        from datetime import date

        from lushu.domain.booking import Severity, lint_rule

        candidate = bs.get_rule(args.poi_id)
        problems = [
            item
            for item in lint_rule(candidate, today=date.today())  # type: ignore[arg-type]
            if item.severity is Severity.ERROR
        ]
        print("复核没过。这条规则现在放出去会误导用户：")
        for item in problems:
            print(f"  {item}")
        if not problems and not (args.evidence or rule.evidence_url):
            print("  ✗ [no_evidence] 没有来源链接：预约规则必须有据可查")
        print()
        print("先补材料（ls booking seed 或直接改种子文件），再复核")
        return 1

    print(f"已复核 {args.poi_id}，复验到期 {bs.get_rule(args.poi_id).verify_due_at}")  # type: ignore[union-attr]
    return 0


def _cmd_booking_ics(args: argparse.Namespace) -> int:
    from datetime import date

    from lushu.services import ics as ics_service

    today = date.fromisoformat(args.today) if args.today else None
    text, skipped, name = ics_service.calendar_for_trip(args.trip_id, today=today)

    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"已写出 {args.out}")
    else:
        print(text, end="")

    if skipped:
        print()
        print("这些景点算不出放票日，没有生成提醒：")
        for item in skipped:
            print(f"  {item}")
        print("要么规则里没写提前天数，要么规则还没复核（草案不对用户可见）")
    else:
        print(f"《{name}》的预约提醒都在里面了。导入手机日历即可。", file=sys.stderr)
    return 0


def _review_all(args: argparse.Namespace) -> int:
    """把体检没有 error 的草案全部复核掉。

    这是**批量签字**，所以先把体检结果摆出来再说签了几条：复核意味着
    「这些规则现在可以对用户可见了」，一旦放出去，错的字段就会让人白跑。
    有 error 的一律跳过——门禁的意义就在这里。
    """
    from datetime import date

    from lushu.domain.booking import RuleStatus, Severity, lint_rule
    from lushu.services import booking_store as bs
    from lushu.store import transaction

    rules = [item for item in bs.all_rules() if item.status is RuleStatus.DRAFT]
    if not rules:
        print("没有待复核的草案")
        return 0

    today = date.today()
    names = bs.poi_names()
    skipped: list[tuple[str, list[str]]] = []
    approved: list[str] = []
    for rule in rules:
        problems = [
            item
            for item in lint_rule(rule, today=today)
            if item.severity is Severity.ERROR
        ]
        if problems:
            skipped.append((names.get(rule.poi_id, rule.poi_id), [p.message for p in problems]))
        else:
            approved.append(rule.poi_id)

    with transaction() as conn:
        done = [poi for poi in approved if bs.review_rule(conn=conn, poi_id=poi, reviewed_at=today)]

    print(f"复核 {len(done)} 条，跳过 {len(skipped)} 条")
    for name, reasons in skipped:
        print(f"  跳过 {name}：{'；'.join(reasons)}")
    if done:
        print()
        print("已对用户可见。每条 90 天后到期复验——预约规则会变，")
        print("湖南博物院 2026-07 刚从「提前 7 天」改成「提前 5 天」就是一个例子。")
    return 0 if not skipped else 1


def _cmd_verify_scan(args: argparse.Namespace) -> int:
    from datetime import date

    from lushu.services import verify
    from lushu.store import connect

    today = date.fromisoformat(args.today) if args.today else date.today()
    window = verify.SOON_DAYS if args.days is None else args.days
    conn = connect()
    try:
        report = verify.scan(conn=conn, today=today, soon_days=window)
        upcoming = verify.next_due(conn=conn, today=today)
        if args.apply:
            changed = verify.mark_due_claims(conn=conn, today=today)
    finally:
        conn.close()

    print(f"复验扫描（{today}，提前 {window} 天提醒）")
    print()

    if not report.items:
        print("没有到期或快到期的。")
    else:
        overdue = report.overdue
        if overdue:
            print(f"已经过期 {len(overdue)} 件——这些正在被用户当成当前信息看：")
            for item in overdue:
                print(f"  {_due_line(item)}")
            print()
        soon = report.soon
        if soon:
            print(f"{window} 天内到期 {len(soon)} 件：")
            for item in soon:
                print(f"  {_due_line(item)}")
            print()

    # 「今天没事」与「以后也没事」不是一回事，所以要说清下一次是什么时候
    if report.overdue:
        print("先把上面过期的那几件核掉，再看下一次。")
    elif upcoming is not None:
        left = (upcoming - today).days
        print(f"下一次到期 {upcoming}（还有 {left} 天）。")
    else:
        print("今天之后没有会到期的了——只有不设期限的那一类（拍照机位等）。")

    hit_rules = len(report.rules)
    print()
    print(
        "扫描范围：已复核的预约规则与未被废弃的结论。"
        f"本次命中 规则 {hit_rules} 条、结论 {len(report.claims)} 条；"
        f"不设期限的结论 {report.timeless} 条（不报不代表没问题，代表这类信息不该过期）。"
    )

    if not args.apply:
        if report.overdue:
            print()
            print("上面这些还是 active 状态。加 --apply 把它们标成待复验：")
            print("  只改状态，不删内容，界面上照样展示并标注「复验已到期」。")
        return 0

    print()
    if changed:
        print(f"已标为待复验 {len(changed)} 条：")
        for claim_id in changed:
            print(f"  {claim_id}")
        print("内容都在，界面上多一个「复验已到期」的标注（设计 4.6：标注而不隐藏）。")
    else:
        print("没有需要改状态的（已经过期的那几条本来就已是待复验）。")

    # 规则不在这个开关的管辖范围内，而且这是有意的：把一条规则打回草案，
    # 用户那边的提醒就消失了，他会以为「这个景点不用预约」——正是本项目
    # 最怕的失败模式。所以规则照常提醒，只是标注「复验已到期」。
    if report.rules and report.overdue:
        print()
        print("预约规则不跟着改状态，这是有意的：打回草案会让用户以为「不用预约」，")
        print("而规则错一个字段就要白跑一趟。它们照常提醒，只是标注复验已到期。")
    return 0


def _due_line(item: DueItem) -> str:
    """一行一件。名字在前，因为翻报告的是人的眼睛，不是 diff 工具。"""
    detail = f"　{item.detail}" if item.detail else ""
    return f"{item.description:<10} {item.due_at}  {item.label}{detail}"


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


# ─── 金标准集与评测 ──────────────────────────────────────────────


def _cmd_eval_gold(args: argparse.Namespace) -> int:
    """看与改金标准集。"""
    from datetime import UTC, datetime

    from lushu.services import gold_store as gs
    from lushu.store import transaction

    now = datetime.now(UTC).isoformat(timespec="seconds")

    if args.add:
        if not args.quote or not args.polarity:
            print("加标注至少要给 --quote 与 --polarity（avoid 或 highlight）")
            return 2
        try:
            with transaction() as conn:
                row = gs.add_label(
                    conn=conn,
                    document_id=args.add,
                    quote=args.quote,
                    polarity=args.polarity,
                    subject_name=args.subject,
                    expected_poi_id=args.poi,
                    facet=args.facet,
                    created_at=now,
                )
        except gs.GoldError as exc:
            print(f"标注没写成：{exc}")
            return 1
        print(f"已加标注 {row.label_id}（正文偏移 {row.char_start}-{row.char_end}，{row.verdict}）")
        print("别忘了最后 ls eval gold --done <素材 id> —— 不标记就不进评测")
        return 0

    if args.remove:
        with transaction() as conn:
            removed = gs.remove_label(conn=conn, label_id=args.remove)
        print("已删除" if removed else "没有这条标注")
        return 0 if removed else 1

    if args.set_poi:
        if not args.poi:
            print("要给出 --poi（高德 POI id）")
            return 2
        with transaction() as conn:
            row = gs.update_label(
                conn=conn,
                label_id=args.set_poi,
                expected_poi_id=args.poi,
                set_poi=True,
                subject_name=args.subject,
                facet=args.facet,
            )
        if row is None:
            print("没有这条标注")
            return 1
        print(f"已把 {row.label_id} 的对齐目标设为 {row.expected_poi_id}")
        print("对齐准确率靠这个字段才判得出来：没有它，命中的条只能算「判不出」")
        return 0

    if args.done:
        with transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM source_document WHERE id = ?", (args.done,)
            ).fetchone()
            if exists is None:
                print(f"没有这篇素材：{args.done}")
                return 1
            gs.mark_annotated(
                conn=conn,
                document_id=args.done,
                model_output_seen=args.seen_model,
                annotated_at=now,
            )
        seen = "看过模型输出" if args.seen_model else "盲标"
        print(f"已记入金标准集（{seen}）")
        if not args.seen_model:
            print("盲标是更可信的标法：先自己读完写结论，再拿模型结果对照")
        return 0

    documents = gs.gold_documents()
    if not documents:
        print("库里还没有素材。先 ls ingest paste 导一篇进来")
        return 0

    stats = gs.gold_stats()
    print(f"素材 {len(documents)} 篇｜已标注 {stats['documents']} 篇｜标注条目 {stats['labels']} 条")
    if stats["documents"] < 30:
        print(f"设计里要的是 30 篇的人工标注集，现在 {stats['documents']} 篇——")
        print("样本不够时评测数字只能当噪声看，报告里会如实标出来")
    print()

    for item in documents:
        mark = "✅" if item.in_gold_set else "  "
        seen = "（看过模型）" if item.model_output_seen else ""
        title = (item.title or "(无标题)")[:28]
        print(
            f"  {mark} {item.document_id}  {title:<30}"
            f" 标注 {item.labeled:>3} 条  候选 {item.prediction_count:>3} 条 {seen}"
        )
        if args.show or args.document == item.document_id:
            for row in gs.labels_for_document(item.document_id):
                subject = row.subject_name or "（未写主体）"
                poi = f" → {row.expected_poi_id}" if row.expected_poi_id else ""
                print(f"        [{row.polarity}] {subject}{poi}：{row.quote[:40]}")
            if item.labeled == 0 and item.in_gold_set:
                print("        （这篇标完了，一条真结论都没有）")

    print()
    print("加标注：ls eval gold --add <素材 id> --quote \"原文片段\" --polarity avoid --subject 故宫")
    return 0


def _cmd_eval_run(args: argparse.Namespace) -> int:
    """跑评测，报出抽取精确率、召回率与对齐准确率。"""
    from lushu.services.evaluation import run_evaluation

    bundle = run_evaluation(document_ids=args.documents)
    report = bundle.report

    if not report.sample_size:
        print("金标准集是空的，没有可评的东西。")
        print("先标注：ls eval gold 看有哪些素材，ls eval gold --add 加条目")
        return 1

    stats = bundle.stats
    print(f"金标准集：{stats['documents']} 篇素材、{stats['labels']} 条人工结论"
          f"（其中 {stats['blind']} 篇盲标）")
    if not bundle.ready:
        print()
        print("⚠ 样本不足 30 篇，下面这些数字**不足以支撑结论**。")
        print("  它们能反映「链路是通的」，但一次调优带来的涨跌可能全在噪声里。")
    print()

    print(f"  抽取召回率    {_pct(report.recall)}"
          f"   （{report.hits}/{report.gold_total} 条人工结论被抽到）")
    print(f"  抽取精确率    {_pct(report.precision)}"
          f"   （抽了 {report.predicted_total} 条，{report.hits} 条对得上）")
    print(f"  对齐准确率    {_pct(report.alignment_accuracy)}"
          f"   （判得出来的 {report.judged} 条里错 {report.misaligned} 条）")
    if report.unaligned:
        print(f"  另有 {report.unaligned} 条压根没挂上 POI——对排程等于不存在，"
              f"已计入对齐错误")
    print()

    for item in report.documents:
        print(f"  {item.document_id}  召回 {_pct(item.recall)}  精确 {_pct(item.precision)}"
              f"  对齐 {_pct(item.alignment_accuracy)}"
              f"  （{item.gold_total} 条人工 / {item.predicted_total} 条预测）")
        for gold in item.pairing.missed[: args.limit]:
            print(f"      漏抽：{gold.quote[:50]}")
        for bad in item.pairing.spurious[: args.limit]:
            print(f"      多抽：{bad.prediction.subject_name} — {bad.reason}")
        for prediction, gold in item.pairing.misaligned[: args.limit]:
            print(f"      挂错：{prediction.subject_name} 挂到 {prediction.poi_id}，"
                  f"标注期望 {gold.expected_poi_id}")

    print()
    print("漏抽与多抽的清单是调提示词的直接依据；对齐错误看 ls align list")
    return 0


def _pct(value: float | None) -> str:
    """比率转百分比。算不出来就写「判不出」，不写 0%——那是两回事。"""
    return "判不出" if value is None else f"{value:6.1%}"


# ─── 导入 ────────────────────────────────────────────────────────


def _cmd_ingest_paste(args: argparse.Namespace) -> int:
    from lushu.services.ingest import ImportKind, ImportRequest, import_document

    if args.file:
        body = args.file.read_text(encoding="utf-8", errors="replace")
    else:
        print("把正文粘进来，按 Ctrl+Z 回车结束：", file=sys.stderr)
        body = sys.stdin.read()

    try:
        result = import_document(
            ImportRequest(
                body=body,
                title=args.title or (args.file.stem if args.file else None),
                url=args.url,
                author=args.author,
                site=args.site,
                kind=ImportKind.PASTE,
            )
        )
    except ValueError as exc:
        print(f"导入失败：{exc}")
        return 1

    _report_import(result)
    return 0


def _cmd_ingest_import(args: argparse.Namespace) -> int:
    from lushu.services.ingest import ImportRequest, import_document, read_source_file

    paths = _expand_paths(args.paths)
    if not paths:
        print("没有找到可导入的文件")
        return 1

    new = repost = exact = 0
    for path in paths:
        try:
            request = read_source_file(path)
        except (OSError, ValueError) as exc:
            print(f"  跳过 {path.name}：{exc}")
            continue
        result = import_document(
            ImportRequest(
                body=request.body,
                title=request.title,
                site=args.site,
                url=request.url,
                kind=request.kind,
            )
        )
        mark = {"new": "新", "repost": "转载", "exact": "重复"}[result.duplicate.value]
        print(f"  [{mark}] {path.name}：{result.chars} 字")
        if result.duplicate.value == "new":
            new += 1
        elif result.duplicate.value == "repost":
            repost += 1
            if result.coverage is not None:
                print(f"        与已有素材重合 {result.coverage:.0%}（{result.duplicate_of}）")
        else:
            exact += 1

    print()
    print(f"入库 {new} 篇，转载 {repost} 篇，完全重复 {exact} 篇")
    if repost:
        print("转载会归到同一个独立来源组——置信度按组计数，不按篇（Q34）")
    return 0


def _cmd_ingest_fetch(args: argparse.Namespace) -> int:
    import httpx

    from lushu.services.ingest import (
        ImportKind,
        ImportRequest,
        detect_site,
        html_to_text,
        import_document,
    )

    # 不碰登录态：只抓公开页面，不带任何凭据，也不跟随到需要登录的跳转
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )
    }
    try:
        response = httpx.get(args.url, headers=headers, timeout=30, follow_redirects=True)
    except httpx.HTTPError as exc:
        print(f"抓取失败：{type(exc).__name__}: {exc}")
        return 1

    if response.status_code != 200:
        print(f"抓取失败：HTTP {response.status_code}")
        return 1

    body = html_to_text(response.text)
    if not body.strip():
        print("抓到的页面没有可用正文（可能需要登录，或正文是脚本渲染的）")
        return 1

    result = import_document(
        ImportRequest(
            body=body,
            url=args.url,
            site=detect_site(args.url),
            kind=ImportKind.FETCH,
        )
    )
    _report_import(result)
    print()
    print(f"正文 {len(body)} 字。抓下来的全文只留在本地底档，路书只带片段")
    return 0


def _report_import(result) -> None:
    mark = {"new": "新素材", "repost": "转载", "exact": "已存在"}[result.duplicate.value]
    print(f"{mark}：{result.document_id}")
    print(f"  站点 {result.site}，正文 {result.chars} 字")
    if result.duplicate.value == "repost":
        print(f"  与 {result.duplicate_of} 重合 {result.coverage:.0%}，归到同一来源组")
    if result.duplicate.value == "exact":
        print("  正文指纹与已有素材相同，没有重复入库")


def _expand_paths(paths: list[Path]) -> list[Path]:
    """把目录展开成文件列表，跳过不是素材的扩展名。"""
    allowed = {".md", ".markdown", ".txt", ".html", ".htm"}
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in allowed))
        elif path.is_file():
            files.append(path)
        else:
            print(f"  跳过不存在的路径：{path}")
    return files


# ─── 提纯 ────────────────────────────────────────────────────────


def _cmd_extract_run(args: argparse.Namespace) -> int:
    from lushu.services.pipeline import extract_documents

    report = extract_documents(
        document_ids=args.documents, limit=args.limit, force=args.force
    )

    print(f"处理 {report.documents} 篇（其中 {report.cached} 篇命中缓存）")
    print(f"模型吐出 {report.candidates} 条候选")
    print(f"  引文校验通过 {report.accepted} 条")
    print(f"  引文对不上原文而丢弃 {report.dropped} 条", end="")
    print(f"（占 {report.drop_ratio:.0%}）" if report.candidates else "")
    if report.loose:
        print(f"  宽松命中 {report.loose} 条 —— 说明这几篇的正文清洗有问题，值得看一眼")
    for document_id, error in report.failed:
        print(f"  失败 {document_id}：{error}")

    print()
    print("通过校验的候选挂在待对齐队列上，跑 ls align run 去对到高德 POI")
    return 0 if not report.failed else 1


def _cmd_group_run(args: argparse.Namespace) -> int:
    from lushu.services.pipeline import DEFAULT_CONCLUSION_OVERLAP, group_by_conclusions

    threshold = args.threshold if args.threshold is not None else DEFAULT_CONCLUSION_OVERLAP
    report = group_by_conclusions(threshold=threshold)

    print(f"比对了 {report.compared} 对素材，合并 {report.merged} 篇")
    print(f"现在共 {report.groups} 个独立来源组")
    print()
    print("这一层按**提纯结果**判同源，认的是逐句改写的洗稿；")
    print("照搬与节选在导入时已经按正文相似度归过组了（ADR-0008）")
    return 0


# ─── 对齐 ────────────────────────────────────────────────────────


def _cmd_align_run(args: argparse.Namespace) -> int:
    from lushu.services.pipeline import align_pending

    report = align_pending(limit=args.limit)

    print(f"处理 {report.mentions} 条提及")
    print(f"  对上并落库 {report.aligned} 条，其中 {report.collapsed} 条由子点归并到本体")
    print(f"  仍然对不上 {report.pending} 条")
    if report.unresolved_subjects:
        print(f"  缺城市线索 {report.unresolved_subjects} 条 —— 补上城市再跑，不猜")
    for name, error in report.failed:
        print(f"  失败「{name}」：{error}")

    print()
    print("对不上的在 ls align list 里，带着候选等人挑（Q19）")
    return 0 if not report.failed else 1


def _cmd_align_list(args: argparse.Namespace) -> int:
    from lushu.services import knowledge_store as ks

    tasks = ks.pending_alignments(limit=args.limit)
    if not tasks:
        print("待对齐队列是空的")
        return 0

    print(f"待对齐 {len(tasks)} 条：")
    for task in tasks:
        city = task.city_adcode or "（缺城市线索）"
        print(f"\n  {task.task_id}  「{task.mention_name}」  城市 {city}")
        print(f"    候选结论 {len(task.claims)} 条")
        for payload in task.claims[:3]:
            print(f"      · {payload.get('text')}")
        if len(task.claims) > 3:
            print(f"      …… 另有 {len(task.claims) - 3} 条")
        if task.candidates:
            print("    高德候选：")
            for item in task.candidates[:5]:
                flag = "" if item.get("usable") else f"  ✗ {item.get('reject_reason')}"
                print(f"      {item.get('poi_id')}  {item.get('name')}{flag}")
        else:
            print("    （没有候选，可能是高德没返回像样的结果）")
    return 0


def _cmd_align_audit(_args: argparse.Namespace) -> int:
    """体检：有没有结论或待办挂到了子点上。

    这条检查是金标准集第一次跑起来时逼出来的：评测报「太和殿 挂错」，
    追下去是旧数据挂在 `故宫博物院-太和殿` 上。`align_pending` 只处理
    pending 的待办，已解析的不会被重新检查——**对齐算法改进了，
    旧结论不会跟着变好**。这个命令就是给这种陈旧数据用的。
    """
    from lushu.services.evaluation import sub_poi_violations

    violations = sub_poi_violations()
    if not violations:
        print("没有挂到子点上的结论或待办，ADR-0009 是干净的")
        return 0

    claims = [item for item in violations if item.kind == "claim"]
    tasks = [item for item in violations if item.kind == "task"]
    print(f"发现 {len(claims)} 条结论、{len(tasks)} 张待办挂在了子点上：")
    for item in violations:
        root = f"{item.root_name}（{item.root_id}）" if item.root_id else "（找不到本体）"
        kind = "结论" if item.kind == "claim" else "待办"
        print(f"\n  [{kind}] 「{item.subject}」挂在 {item.poi_name}（{item.poi_id}）")
        print(f"         本体应该是 {root}")
        print(f"         {item.ref_id}")
    print()
    print("ADR-0009 要求结论一律挂本体。要不要挪，得看这条结论说的是子点自己的事")
    print("（「太和殿要另外买票」）还是整个本体的事——这需要人判断，不自动挪。")
    print("做法：把这张待办退回 pending 再跑一次 ls align run，")
    print("旧结论该删的要先删掉（ls align show <poi_id> 能查出来）。")
    return 1


def _cmd_align_merge_pois(args: argparse.Namespace) -> int:
    """把同一处地方的两个实体并成一个。

    ADR-0002 说高德是实体真源，一个地方只该有一行 `poi`。M1/M2 落库时用的是
    引擎回填的 id，M3 有了自己的适配器之后又按名称搜了一遍，于是同一处地方
    有了两行。不合并的话，预约规则与攻略结论都挂在新实体上，
    而旧行程的天项指向旧实体——**规则与结论永远到不了那些行程**。
    """
    from lushu.services import poi_merge
    from lushu.store import connect, transaction

    if not args.apply:
        groups = poi_merge.find_duplicates(connect())
        if not groups:
            print("没有同名多行的实体，ADR-0002 是干净的")
            return 0
        print(f"发现 {len(groups)} 处地方各有两行（或更多）：")
        print()
        for group in groups:
            print(f"  {group.summary}")
            for item in group.losers:
                counts = poi_merge.reference_counts(connect(), item.poi_id)
                detail = "、".join(f"{k} {v} 行" for k, v in counts.items()) or "没有引用"
                print(f"        并掉的这行被指着：{detail}")
            print()
        print("这只是报告。真要合并加 --apply——合并不可逆，先看清楚要动什么。")
        return 1

    with transaction() as conn:
        groups, report = poi_merge.merge_duplicates(conn=conn)

    if not groups:
        print("没有同名多行的实体，没什么可合并的")
        return 0

    print(f"并掉 {report.merged} 行旧实体（{len(groups)} 处地方）：")
    for key, value in sorted(report.moved.items()):
        print(f"  {key}：改了 {value} 行")
    print()
    print("指向旧实体的引用都已改到留下的那行上。")
    return 0


def _cmd_align_recheck(args: argparse.Namespace) -> int:
    """重新对齐：看哪些结论的落点在算法改进后已经过时。

    `align_pending` 只处理待办，一条待办解析之后就不会被重新检查，
    所以**对齐算法的每一次改进都只对新数据生效**。这个命令把库里已有的
    结论按现在的算法重跑一遍，把落点变了的报出来。
    """
    from lushu.services import realign
    from lushu.store import transaction

    print("按现在的对齐算法把库里每条提及重跑一遍……")
    plans = realign.survey()
    changed = [item for item in plans if item.changed]

    if not changed:
        print()
        print(f"检查了 {len(plans)} 条提及，落点都没有变化")
        return 0

    print()
    print(f"{len(changed)} 条提及的落点变了（共 {len(plans)} 条）：")
    for item in sorted(changed, key=lambda p: -p.reference_count):
        print()
        print(f"  「{item.subject_name}」"
              f"{item.claim_count} 条结论、{item.item_count} 个行程天项")
        print(f"    现在挂在 {item.current_name}（{item.current_poi_id}）")
        print(f"    重跑会挂到 {item.new_name}（{item.new_poi_id}）")

    if not args.apply:
        print()
        print("这只是报告。要真搬加 --apply——搬一条结论等于替用户改了他要去的地方，")
        print("而且不可逆，所以先看清楚。")
        return 1

    with transaction() as conn:
        report = realign.apply_plans(changed, conn=conn)

    print()
    print(f"搬了 {report.moved_claims} 条结论、{report.moved_items} 个行程天项"
          f"（{report.moved_subjects} 条提及），并重算了独立来源数")
    if report.moved_items:
        print("行程天项的标题没动——那行字是人看到的景点名，与它指向哪个实体是两回事。")
    for name, why in report.skipped:
        print(f"  跳过「{name}」：{why}")
    return 0


def _cmd_align_show(args: argparse.Namespace) -> int:
    from lushu.services import knowledge_store as ks

    claims = ks.claims_for_poi(args.poi_id)
    if not claims:
        print(f"{args.poi_id} 上还没有结论")
        return 0

    print(f"{args.poi_id} 上有 {len(claims)} 条结论：")
    for claim in claims:
        tag = "高置信" if claim.confidence.value == "high" else "待验证个例"
        print(f"\n  [{tag} / {claim.polarity} / {claim.facet}] {claim.text}")
        print(f"    独立来源 {claim.independent_source_count} 个，证据 {claim.evidence_count} 条")
        for evidence in ks.evidence_for_claim(claim.claim_id)[:3]:
            print(f"      · {evidence['quote'][:60]}")
            print(f"        来源：{evidence['site']} {evidence['url'] or ''}")
    return 0


# ─── 整条链路 ────────────────────────────────────────────────────


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    from lushu.services.pipeline import (
        align_pending,
        extract_documents,
        group_by_conclusions,
        merge_claims,
        pipeline_stats,
    )

    print("① 提纯")
    extracted = extract_documents(limit=args.limit)
    print(f"   {extracted.documents} 篇，通过校验 {extracted.accepted} 条，"
          f"丢弃 {extracted.dropped} 条")
    for document_id, error in extracted.failed:
        print(f"   失败 {document_id}：{error}")

    print("② 对齐")
    aligned = align_pending(limit=args.limit)
    print(f"   对上 {aligned.aligned} 条（含 {aligned.collapsed} 条归并到本体），"
          f"仍待人工 {aligned.pending} 条，缺城市线索 {aligned.unresolved_subjects} 条")
    for name, error in aligned.failed:
        print(f"   失败「{name}」：{error}")

    print("③ 归组（按结论重合度）")
    grouped = group_by_conclusions()
    print(f"   合并 {grouped.merged} 篇，现有 {grouped.groups} 个来源组")

    print("④ 合并同义结论并重算置信度")
    merged = merge_claims()
    print(f"   结论 {merged.created} 条，其中高置信 {merged.high_confidence} 条，"
          f"待验证个例 {merged.single_source} 条")

    print()
    stats = pipeline_stats()
    print("链路现状：")
    for key in ("documents", "groups", "claims", "high_confidence", "evidence"):
        print(f"  {key:<18}{stats[key]}")
    print(f"  {'待对齐':<17}{stats['align_pending']}")
    print(f"  {'引文丢弃合计':<15}{stats['extract_dropped']}")
    print()
    print("下一步：ls align list 处置对不上的提及；ls eval gold 看金标准集标到哪了")
    return 0


def _cmd_stats(_args: argparse.Namespace) -> int:
    from lushu.services.evaluation import alignment_tally
    from lushu.services.gold_store import gold_stats
    from lushu.services.pipeline import pipeline_stats

    stats = pipeline_stats()
    labels = {
        "documents": "素材篇数",
        "groups": "独立来源组",
        "claims": "结论条数",
        "high_confidence": "高置信结论",
        "evidence": "证据条数",
        "extract_runs": "提纯运行次数",
        "extract_documents": "已提纯篇数",
        "extract_candidates": "模型吐出候选",
        "extract_accepted": "引文校验通过",
        "extract_dropped": "引文丢弃",
        "align_pending": "待对齐",
        "align_resolved": "已对齐",
        "align_discarded": "已判定非地点",
    }
    for key, label in labels.items():
        print(f"  {label:<16}{stats.get(key, 0)}")

    # 金标准集与人工处置的进度放在最后：它们是「链路之外」的两件事，
    # 但决定了上面这些数字能不能被信任
    gold = gold_stats()
    tally = alignment_tally()
    gap = f"（还差 {30 - gold['documents']} 篇到 30 篇）" if gold["documents"] < 30 else ""
    print()
    print(f"  {'金标准集篇数':<14}{gold['documents']}{gap}")
    print(f"  {'人工标注条目':<14}{gold['labels']}")
    print(f"  {'盲标篇数':<15}{gold['blind']}（盲标更可信：先自己读完写结论）")
    discard = f"{tally.discard_ratio:.0%}" if tally.discard_ratio is not None else "判不出"
    print(f"  {'人工处置待办':<14}{tally.total}（其中判为非地点 {tally.discarded}，占 {discard}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
