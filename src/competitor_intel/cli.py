"""CLI 入口（TDD 7.3）。

一个入口三种形态：
- `ci run` / `ci report` / `ci review`：内核，一次性任务；
- `ci mcp`：stdio MCP 服务器（AI 客户端按需调用）；
- `ci serve [--schedule]`：HTTP API，可选带定时调度。
"""

import asyncio
from typing import Optional

import typer

app = typer.Typer(help="竞品情报系统 CLI", no_args_is_help=True)


def _settings():
    from .core.config import Settings

    return Settings.from_env()


@app.command("init-db")
def init_db(db_path: Optional[str] = typer.Option(None, help="SQLite 路径")) -> None:
    """初始化数据库。"""
    from .core.store import init_db as _init_db

    path = db_path or str(_settings().db_path)
    asyncio.run(_init_db(path))
    typer.echo(f"数据库已初始化：{path}")


@app.command("run")
def run(
    config: Optional[str] = typer.Option(None, help="站点配置"),
    competitor: Optional[str] = typer.Option(None, help="只跑指定竞品"),
    force: bool = typer.Option(False, help="强制忽略指纹"),
) -> None:
    """单次全量运行（仍会走指纹短路）。"""
    from .core.graph import run_pipeline

    config = config or str(_settings().targets_path)
    state = asyncio.run(run_pipeline(config, competitor=competitor, force=force))
    results = state.get("results", [])
    extracted = sum(1 for r in results if r.status == "extracted")
    extraction_errors = sum(1 for r in results if r.status == "extraction_error")
    typer.echo(
        f"完成：extracted={extracted} skipped={len(state.get('skipped', []))} "
        f"extraction_error={extraction_errors} "
        f"errors={len(state.get('errors', []))} blocked={len(state.get('blocked', []))}"
    )
    for err in state.get("errors", []):
        typer.echo(f"  [error] {err}")
    typer.echo(f"报告：{state.get('report_path')}")


@app.command("review")
def review(
    config: Optional[str] = typer.Option(None, help="站点配置"),
    db_path: Optional[str] = typer.Option(None, help="SQLite 路径"),
    retry_failed: bool = typer.Option(False, help="重跑复核队列中的失败记录"),
) -> None:
    """查看 / 重跑人工复核队列。"""
    from .core.store import list_review_queue

    settings = _settings()
    config = config or str(settings.targets_path)
    db_path = db_path or str(settings.db_path)

    rows = asyncio.run(list_review_queue(db_path))
    if not rows:
        typer.echo("复核队列为空")
        return

    if not retry_failed:
        typer.echo(f"待复核 {len(rows)} 条：")
        for r in rows:
            typer.echo(
                f"  [{r['snapshot_date']}] {r['competitor']} {r['page_url']}\n"
                f"    原因：{r['error_detail']}"
            )
        typer.echo("使用 --retry-failed 强制重跑这些页面")
        return

    from pathlib import Path

    from .core.collector import fetch_and_extract
    from .core.config import iter_targets, load_config

    settings.db_path = Path(db_path)
    failed_keys = {(r["competitor"], r["page_url"]) for r in rows}
    targets = [
        t for t in iter_targets(load_config(config)) if (t.competitor, t.url) in failed_keys
    ]
    if not targets:
        typer.echo("失败记录在配置中无匹配目标")
        return

    async def _retry() -> None:
        for t in targets:
            result = await fetch_and_extract(t, force=True, settings=settings)
            typer.echo(f"  {t.competitor} {t.url} -> {result.status}")

    typer.echo(f"强制重跑 {len(targets)} 个页面：")
    asyncio.run(_retry())


@app.command("report")
def report(
    since: str = typer.Option(..., help="起始日期 YYYY-MM-DD"),
    out: Optional[str] = typer.Option(None, help="输出路径"),
    db_path: Optional[str] = typer.Option(None, help="SQLite 路径"),
) -> None:
    """从快照重新生成 Markdown 报告。"""
    from .core.diff import diff_snapshots
    from .core.models import CompetitorSnapshot
    from .core.reporter import render_report
    from .core.store import latest_snapshot, snapshots_since

    settings = _settings()
    db_path = db_path or str(settings.db_path)
    out = out or str(settings.report_dir / "latest.md")

    async def _collect():
        snaps = await snapshots_since(db_path, since)
        # 每个页面取窗口内最新快照作为当前状态
        latest: dict[tuple[str, str], CompetitorSnapshot] = {}
        for s in snaps:
            latest[(s.competitor_name, s.page_url)] = s
        rows = list(latest.values())
        diffs = []
        for s in rows:
            baseline = await latest_snapshot(
                db_path, s.competitor_name, s.page_url, before=s.snapshot_date
            )
            if baseline is None:
                continue
            d = diff_snapshots(baseline, s)
            if d.changed:
                diffs.append(d)
        return rows, diffs

    rows, diffs = asyncio.run(_collect())
    path = render_report(rows, diffs, out=out)
    typer.echo(f"报告已生成：{path}")


@app.command("mcp")
def mcp() -> None:
    """以 stdio 启动 MCP 服务器（供 Claude Desktop / Cursor 等客户端拉起）。"""
    from .adapters.mcp.server import main

    main()


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
    reload: bool = typer.Option(False, help="开发模式自动重载"),
    schedule: bool = typer.Option(False, help="同时启动 Celery worker + beat（需 Redis）"),
) -> None:
    """启动 HTTP 服务；`--schedule` 时一并启动定时采集。"""
    import subprocess
    import sys

    import uvicorn

    worker = None
    if schedule:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "competitor_intel.adapters.schedule.tasks:celery_app",
                "worker",
                "-B",
                "-l",
                "info",
            ]
        )
    try:
        uvicorn.run(
            "competitor_intel.adapters.http.api:app",
            host=host,
            port=port,
            reload=reload,
        )
    finally:
        if worker is not None:
            worker.terminate()
            worker.wait(timeout=10)


if __name__ == "__main__":
    app()
