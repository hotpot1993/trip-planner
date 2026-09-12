"""命令行入口。命令名 `ls`（路书）。

M0 阶段提供 `serve`、`init-db`、`doctor`。M3 阶段加入数据链路的子命令。

`eval`（金标准评测）与 M4 的 `booking`、`verify` 还没做，这里**不注册空壳**——
命令列表应当如实反映能力，而不是列出一堆点了就报错的入口。
计划中的命令写在 `serve` 帮助的末尾备查。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lushu import __version__, config

PLANNED_PIPELINE = """\
还没做的子命令（M3 余下部分与 M4）：
  eval gold | run                 金标准标注与提纯质量评测
  verify scan                     复验到期扫描
  booking seed | lint             预约规则种子库与体检
  poi warm                        按城市预热高德 POI
"""

# 导入时提示的站点标识，与 source_document.site 的取值一致
_SITE_CHOICES = ("mafengwo", "zhihu", "qiongyou", "ctrip", "xhs", "manual")


def main(argv: list[str] | None = None) -> int:
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

    align.set_defaults(_handler=lambda _args: _usage(align))


def _register_pipeline(sub) -> None:
    pipeline = sub.add_parser("pipeline", help="整条链路")
    actions = pipeline.add_subparsers(dest="action")

    run = actions.add_parser("run", help="导入后的全流程：提纯 → 对齐 → 归组 → 合并")
    run.add_argument("--limit", type=int, default=50, help="每步最多处理几篇，默认 50")
    run.set_defaults(_handler=_cmd_pipeline_run)

    pipeline.set_defaults(_handler=lambda _args: _usage(pipeline))


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


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


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

    report = extract_documents(document_ids=args.documents, limit=args.limit)

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
    return 0


def _cmd_stats(_args: argparse.Namespace) -> int:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
