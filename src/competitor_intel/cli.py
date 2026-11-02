"""CLI 入口（TDD 7.3）。

子命令：init-db / run / review / report
"""

import asyncio
from typing import Optional

import typer

app = typer.Typer(help="竞品情报系统 CLI", no_args_is_help=True)


@app.command("init-db")
def init_db(db_path: str = typer.Option("data/snapshots.db", help="SQLite 路径")) -> None:
    """初始化数据库。"""
    from .store import init_db as _init_db

    asyncio.run(_init_db(db_path))
    typer.echo(f"数据库已初始化：{db_path}")


@app.command("run")
def run(
    config: str = typer.Option("config/targets.yaml", help="站点配置"),
    competitor: Optional[str] = typer.Option(None, help="只跑指定竞品"),
    force: bool = typer.Option(False, help="强制忽略指纹"),
) -> None:
    """单次全量运行（仍会走指纹短路）。"""
    from .graph import run_pipeline

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
    config: str = typer.Option("config/targets.yaml", help="站点配置"),
    db_path: str = typer.Option("data/snapshots.db", help="SQLite 路径"),
    retry_failed: bool = typer.Option(False, help="重跑复核队列中的失败记录"),
) -> None:
    """查看 / 重跑人工复核队列。"""
    from .store import list_review_queue

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

    from .collector import fetch_and_extract
    from .config import Settings, iter_targets, load_config

    settings = Settings.from_env()
    settings.db_path = db_path
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
    out: str = typer.Option("data/reports/latest.md", help="输出路径"),
    db_path: str = typer.Option("data/snapshots.db", help="SQLite 路径"),
) -> None:
    """从快照重新生成 Markdown 报告。"""
    from .diff import diff_snapshots
    from .models import CompetitorSnapshot
    from .reporter import render_report
    from .store import latest_snapshot, snapshots_since

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


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
    reload: bool = typer.Option(False, help="开发模式自动重载"),
) -> None:
    """启动 HTTP 服务（采集由 Celery worker 执行）。"""
    import uvicorn

    uvicorn.run("competitor_intel.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
