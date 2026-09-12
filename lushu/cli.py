"""命令行入口。命令名 `ls`（路书）。

M0 阶段提供 `serve`、`init-db`、`doctor` 三个子命令。

数据管线的子命令（`ingest`、`extract`、`group`、`align`、`eval`、`verify`、
`booking`、`poi`）属于 M3 与 M4 阶段。这里刻意不注册空壳子命令——
命令列表应当如实反映能力，而不是列出一堆点了就报错的入口。
计划中的命令写在 `serve` 帮助的末尾备查。
"""

from __future__ import annotations

import argparse
import sys

from lushu import __version__, config

PLANNED_PIPELINE = """\
计划中的管线子命令（M3 与 M4 阶段加入）：
  ingest paste | import | fetch   导入攻略素材
  extract run                     批量提纯
  group run                       独立来源归组（域名 + 作者 + 正文相似度）
  align run                       实体对齐（落到高德 POI）
  eval gold | run                 金标准标注与提纯质量评测
  verify scan                     复验到期扫描
  booking seed | lint             预约规则种子库与体检
  poi warm                        按城市预热高德 POI
"""


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

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "serve":
        return _serve(args)
    if args.command == "init-db":
        return _init_db()
    if args.command == "doctor":
        return _doctor()
    return 2


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


if __name__ == "__main__":
    raise SystemExit(main())
