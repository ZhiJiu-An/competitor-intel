"""LangGraph 编排：plan -> fetch -> diff -> analyze -> report（TDD 4.1 / 7.2）。

设计要点：
- fetch 节点每次处理一个目标并 checkpoint，崩溃后从下一个目标续跑；
- 单目标失败只记 state["errors"] 并继续批次，不拖垮整批（tenacity 2 次指数退避后放弃）；
- 状态字段采用整体替换语义：plan 重置累积字段，同日重跑得到干净结果；
- checkpointer 用 AsyncSqliteSaver（与业务库同库不同表），thread_id=当天日期。
"""

import inspect
from datetime import date
from typing import Optional, TypedDict

import structlog
from langgraph.graph import END, StateGraph
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .analyst import analyze_diff
from .collector import FetchResult, fetch_and_extract
from .config import Defaults, Settings, Target, iter_targets, load_config
from .diff import DiffResult, diff_snapshots
from .models import CompetitorSnapshot
from .reporter import render_report
from .store import init_db, latest_snapshot

log = structlog.get_logger(__name__)


class PipelineState(TypedDict, total=False):
    config_path: str
    competitor_filter: Optional[str]
    force: bool
    defaults: Defaults
    targets: list[Target]
    results: list[FetchResult]
    diffs: list[DiffResult]
    analyses: list[str]
    report_path: Optional[str]
    skipped: list[str]
    errors: list[str]  # 重试后仍失败的目标
    blocked: list[str]  # 合规拦截


def build_graph(settings: Settings, *, checkpointer=None):
    """组装 StateGraph 与条件边。settings 通过闭包注入各节点。"""

    async def _plan(state: PipelineState) -> dict:
        config = load_config(state["config_path"])
        targets = iter_targets(config)
        cf = state.get("competitor_filter")
        if cf:
            targets = [t for t in targets if t.competitor == cf]
        await init_db(settings.db_path)
        log.info("pipeline.plan", targets=len(targets))
        # 重置累积字段：同日重跑从干净状态开始
        return {
            "targets": targets,
            "defaults": config.defaults,
            "results": [],
            "diffs": [],
            "analyses": [],
            "skipped": [],
            "errors": [],
            "blocked": [],
            "report_path": None,
        }

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _fetch_once(target: Target, *, force: bool, defaults: Defaults) -> FetchResult:
        return await fetch_and_extract(target, force=force, settings=settings, defaults=defaults)

    async def _fetch(state: PipelineState) -> dict:
        targets = list(state.get("targets", []))
        if not targets:
            return {}
        target, remaining = targets[0], targets[1:]
        key = f"{target.competitor} {target.url}"
        update: dict = {"targets": remaining}
        try:
            result = await _fetch_once(
                target,
                force=state.get("force", False),
                defaults=state.get("defaults") or Defaults(),
            )
        except Exception as exc:  # 重试后仍失败：记录并继续批次
            log.error("fetch.failed", url=target.url, competitor=target.competitor, error=str(exc))
            update["errors"] = state.get("errors", []) + [f"{key}: {exc}"]
            return update

        update["results"] = state.get("results", []) + [result]
        if result.status == "skipped":
            update["skipped"] = state.get("skipped", []) + [key]
        elif result.status == "blocked":
            update["blocked"] = state.get("blocked", []) + [key]
        return update

    def _route_after_fetch(state: PipelineState) -> str:
        return "fetch" if state.get("targets") else "diff"

    async def _diff(state: PipelineState) -> dict:
        today = date.today().isoformat()
        diffs: list[DiffResult] = []
        for r in state.get("results", []):
            if r.status != "extracted" or r.snapshot is None:
                continue
            baseline = await latest_snapshot(
                settings.db_path, r.target.competitor, r.target.url, before=today
            )
            if baseline is None:
                continue  # 首次采集无基线，不入 diff
            d = diff_snapshots(baseline, r.snapshot)
            if d.changed:
                diffs.append(d)
        log.info("pipeline.diff", changed=len(diffs))
        return {"diffs": diffs}

    async def _analyze(state: PipelineState) -> dict:
        analyses: list[str] = []
        for d in state.get("diffs", []):
            try:
                analyses.append(await analyze_diff(d, settings=settings))
            except Exception as exc:
                log.warning("analyze.degraded", competitor=d.competitor, error=str(exc))
                analyses.append("（变更解读生成失败，请人工查看上方变更表）")
        return {"analyses": analyses}

    async def _report(state: PipelineState) -> dict:
        rows: list[CompetitorSnapshot] = [
            r.snapshot
            for r in state.get("results", [])
            if r.status == "extracted" and r.snapshot is not None
        ]
        path = render_report(
            rows,
            state.get("diffs", []),
            state.get("analyses", []),
            out_dir=settings.report_dir,
        )
        log.info("pipeline.report", path=str(path))
        return {"report_path": str(path)}

    g = StateGraph(PipelineState)
    g.add_node("plan", _plan)
    g.add_node("fetch", _fetch)
    g.add_node("diff", _diff)
    g.add_node("analyze", _analyze)
    g.add_node("report", _report)
    g.set_entry_point("plan")
    g.add_edge("plan", "fetch")
    g.add_conditional_edges("fetch", _route_after_fetch, {"fetch": "fetch", "diff": "diff"})
    g.add_edge("diff", "analyze")
    g.add_edge("analyze", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


async def _make_checkpointer(db_path: str):
    """直接构造 AsyncSqliteSaver，规避各版本 from_conn_string 的签名漂移。"""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = aiosqlite.connect(db_path)
    saver = AsyncSqliteSaver(conn)
    setup = getattr(saver, "setup", None)
    if setup is not None:
        maybe_awaitable = setup()
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable
    return saver, conn


async def run_pipeline(
    config_path: str,
    *,
    competitor: str | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> PipelineState:
    """单次全量运行入口：建图 -> 执行 -> 返回最终状态。"""
    settings = settings or Settings.from_env()
    _, conn = await _make_checkpointer(settings.db_path)
    try:
        graph = build_graph(settings, checkpointer=_)
        initial: PipelineState = {
            "config_path": str(config_path),
            "competitor_filter": competitor,
            "force": force,
        }
        return await graph.ainvoke(
            initial,
            config={"configurable": {"thread_id": date.today().isoformat()}},
        )
    finally:
        await conn.close()
