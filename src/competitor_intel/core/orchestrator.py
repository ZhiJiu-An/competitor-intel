"""服务层：把 run_pipeline 包成可幂等、可查询的运行记录（P1 / TDD 7.4）。

职责边界：
- 只做「创建 run -> 执行 -> 记录结果」这一层包装；
- 不改 LangGraph 图内部逻辑，CLI 与 API 因此共用同一条执行路径；
- 幂等键 = 日期 + 竞品（或 all），同日重复触发返回同一 run，不重复抓取。
"""

import json
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import structlog

from .collector import fetch_and_extract
from .config import Defaults, Settings, Target, load_config
from .diff import diff_snapshots
from .graph import run_pipeline
from .netguard import ensure_allowed
from .reporter import render_report
from .store import create_run, init_db, latest_snapshot, update_run

log = structlog.get_logger(__name__)

DEFAULT_CONFIG = "config/targets.yaml"

#: 推送钩子：由 adapter 注入（core 不 import adapters）
Notifier = Callable[[dict], None]


def idempotency_key(competitor: str | None, day: str | None = None) -> str:
    return f"{day or date.today().isoformat()}:{competitor or 'all'}"


def summarize(state: Mapping[str, Any]) -> dict[str, Any]:
    """从 LangGraph 最终状态计算计数与产品指标。

    指标口径：分母是该批次实际处理的目标数（results 长度），
    不是配置总数——失败在 fetch 阶段就退出的目标不计入 skip_rate。
    """
    results = state.get("results") or []
    processed = len(results)
    extracted = sum(1 for r in results if r.status == "extracted")
    skipped = sum(1 for r in results if r.status == "skipped")
    blocked = sum(1 for r in results if r.status == "blocked")
    extraction_error = sum(1 for r in results if r.status == "extraction_error")
    fallback = sum(1 for r in results if getattr(r, "used_fallback", False))
    errors = len(state.get("errors") or [])

    def _rate(numerator: int) -> float:
        return round(numerator / processed, 4) if processed else 0.0

    return {
        "processed": processed,
        "extracted": extracted,
        "skipped": skipped,
        "blocked": blocked,
        "extraction_error": extraction_error,
        "fallback": fallback,
        "errors": errors,
        "metrics": {
            "skip_rate": _rate(skipped),
            "fallback_rate": _rate(fallback),
            "extraction_error_rate": (
                round(extraction_error / (extracted + extraction_error), 4)
                if (extracted + extraction_error)
                else 0.0
            ),
        },
    }


def decide_status(counts: dict) -> str:
    """succeeded / partial / failed 三态。

    partial 是必要状态：P0 支持「单目标失败不拖垮整批」，
    若沿用二值状态，会把「8 成功 1 失败」误报成 failed。
    注意「全部 skipped」是正常结果（页面无变化），不能判失败。
    """
    if not counts.get("processed", 1):
        return "failed"  # 空批次：配置或筛选没命中任何目标
    if counts["extracted"] == 0 and (counts["errors"] or counts["extraction_error"]):
        return "failed"  # 有目标但一条都没抽到
    if counts["errors"] or counts["extraction_error"] or counts["blocked"]:
        return "partial"
    return "succeeded"


async def enqueue_run(
    settings: Settings,
    *,
    competitor: str | None = None,
    force: bool = False,
    config_path: str = DEFAULT_CONFIG,
) -> tuple[dict, bool]:
    """创建（或复用）运行记录。返回 (记录, 是否需要投递执行)。

    同日同竞品幂等复用；但上一轮若为 failed（如密钥过期、全站超时），
    允许当天重试：把记录复位为 queued 并重新投递，run_id 保持不变。
    force=True 时即使上一轮已结束（succeeded / partial）也重跑，用于
    「我已经改过配置/换了 key，再跑一次」；正在运行的 run 不会被抢占。
    """
    await init_db(settings.db_path)
    record, created = await create_run(
        settings.db_path,
        run_id=uuid.uuid4().hex,
        idempotency_key=idempotency_key(competitor),
        competitor=competitor,
    )
    if (
        not created
        and record["status"] in ("succeeded", "partial", "failed")
        and (force or record["status"] == "failed")
    ):
        await update_run(
            settings.db_path,
            record["id"],
            status="queued",
            counts_json="{}",
            error_summary=None,
            report_path=None,
            started_at=None,
            finished_at=None,
        )
        record["status"] = "queued"
        return record, True
    return record, created


async def execute_run(
    settings: Settings,
    *,
    run_id: str,
    config_path: str | None = None,
    competitor: str | None = None,
    force: bool = False,
    notify: Notifier | None = None,
) -> dict:
    """执行一次采集并回写 run 记录；异常也要落状态，保证失败可见。"""
    config_path = config_path or str(settings.targets_path)
    await update_run(
        settings.db_path, run_id, status="running", started_at=_now()
    )
    try:
        state = await run_pipeline(
            config_path, competitor=competitor, force=force, settings=settings
        )
    except Exception as exc:  # 进程级失败：记录后继续抛出，交由 Celery 决定重试
        await update_run(
            settings.db_path,
            run_id,
            status="failed",
            error_summary=str(exc)[:500],
            finished_at=_now(),
        )
        raise

    counts = summarize(state)
    status = decide_status(counts)
    if status == "failed" and counts["errors"] == 0 and counts["extraction_error"]:
        summary = "全部目标抽取失败，请查看 review_queue"
    else:
        summary = None
    await update_run(
        settings.db_path,
        run_id,
        status=status,
        counts_json=json.dumps(counts, ensure_ascii=False),
        report_path=state.get("report_path"),
        error_summary=summary,
        finished_at=_now(),
    )
    result = {"run_id": run_id, "status": status, **counts, "report_path": state.get("report_path")}
    # 把字段级变化一并交给推送层：卡片要能直接展示明细，而不是只给计数。
    result["differences"] = [
        diff.model_dump() for diff in state.get("diffs") or []
    ]

    # 推送（任何异常静默吞掉，绝不影响主流程）；钩子由 adapter 注入
    if notify is not None:
        try:
            notify(result)
        except Exception as exc:
            log.warning("notify.failed", error=str(exc))

    return result


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 临时抓取（MCP「眼睛」）

ADHOC_SUBDIR = "adhoc"
_SLUG_RE = re.compile(r"[^0-9A-Za-z._-]+")


def default_competitor(url: str) -> str:
    """没给竞品名时用域名兜底，保证快照仍可按 competitor 查询。"""
    host = urlparse(url).netloc.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host or "unknown"


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name).strip("-") or "page"


def _load_defaults(settings: Settings) -> Defaults:
    try:
        return load_config(settings.targets_path).defaults
    except Exception:  # 配置缺失时用内置默认值，不阻断临时抓取
        return Defaults()


async def crawl_page(
    settings: Settings,
    *,
    url: str,
    competitor: str | None = None,
    page_type: str = "pricing",
    main_selector: str | None = None,
    force: bool = False,
) -> dict:
    """临时抓取任意页面：抓取 -> 抽取 -> 与上次快照对比 -> 生成临时报告。

    与定时任务的差别：只处理这一页、不用 targets.yaml、不做 LLM 解读，
    适合 AI「现在就看一眼这个页面」。结果仍写入快照库，因此天然可追溯。
    """
    competitor = competitor or default_competitor(url)
    ensure_allowed(url, allow_private=settings.allow_private_networks)
    # 临时抓取可能发生在从未执行过 init-db 的新环境，先建表再查基线
    await init_db(settings.db_path)
    target = Target(
        competitor=competitor,
        page_type=page_type,
        url=url,
        main_selector=main_selector or "body",
    )

    # 基线必须在抓取前取：同一天重复抓取会覆盖当天那一条，抓完就取不到旧版了
    baseline = await latest_snapshot(settings.db_path, competitor, url)
    result = await fetch_and_extract(
        target,
        force=force,
        settings=settings,
        defaults=_load_defaults(settings),
        origin="adhoc",
    )

    diffs = []
    if result.status == "extracted" and result.snapshot is not None and baseline is not None:
        diff = diff_snapshots(baseline, result.snapshot)
        if diff.changed:
            diffs.append(diff)

    # 指纹未变时上游不会返回快照；「眼睛」场景下这等于没数据，
    # 因此回落到基线，至少让调用方拿到结构化内容。
    snapshot = result.snapshot
    note: str | None = None
    if result.status == "skipped" and baseline is not None:
        snapshot = baseline
        note = "页面内容与上次一致（指纹未变），返回最近一次快照；需要重新抽取请传 force=true"

    report: dict | None = None
    if result.snapshot is not None:
        # page_type 来自外部入参，必须 slug，避免 "../" 把报告写出 report_dir
        rel = (
            f"{ADHOC_SUBDIR}/{date.today().isoformat()}-"
            f"{_slug(competitor)}-{_slug(page_type)}.md"
        )
        path = render_report(
            [result.snapshot],
            diffs,
            ["临时抓取：仅做字段级对比，未调用 LLM 生成解读。"],
            out=Path(settings.report_dir) / rel,
        )
        report = {
            "path": str(path),
            "name": rel,
            "url": f"{settings.report_base_url.rstrip('/')}/reports/{quote(rel, safe='/')}",
        }

    response = {
        "competitor": competitor,
        "page_url": url,
        "page_type": page_type,
        "status": result.status,
        "used_fallback": result.used_fallback,
        "snapshot_date": snapshot.snapshot_date if snapshot else None,
        "baseline_date": baseline.snapshot_date if baseline else None,
        "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        "changes": [change.model_dump() for diff in diffs for change in diff.changes],
        "error_detail": result.error_detail,
        "report": report,
    }
    if result.status == "blocked":
        response["note"] = "robots.txt 不允许抓取该页面，未写入数据库"
    elif note:
        response["note"] = note
    return response
