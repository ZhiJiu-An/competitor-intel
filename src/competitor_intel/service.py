"""服务层：把 run_pipeline 包成可幂等、可查询的运行记录（P1 / TDD 7.4）。

职责边界：
- 只做「创建 run -> 执行 -> 记录结果」这一层包装；
- 不改 LangGraph 图内部逻辑，CLI 与 API 因此共用同一条执行路径；
- 幂等键 = 日期 + 竞品（或 all），同日重复触发返回同一 run，不重复抓取。
"""

import json
import uuid
from datetime import date, datetime

import structlog

from .config import Settings
from .graph import run_pipeline
from .store import create_run, init_db, update_run

log = structlog.get_logger(__name__)

DEFAULT_CONFIG = "config/targets.yaml"


def idempotency_key(competitor: str | None, day: str | None = None) -> str:
    return f"{day or date.today().isoformat()}:{competitor or 'all'}"


def summarize(state: dict) -> dict:
    """从 LangGraph 最终状态计算计数与 MVP §10 指标。

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
    """
    await init_db(settings.db_path)
    record, created = await create_run(
        settings.db_path,
        run_id=uuid.uuid4().hex,
        idempotency_key=idempotency_key(competitor),
        competitor=competitor,
    )
    if not created and record["status"] == "failed":
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
    config_path: str = DEFAULT_CONFIG,
    competitor: str | None = None,
    force: bool = False,
) -> dict:
    """执行一次采集并回写 run 记录；异常也要落状态，保证失败可见。"""
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

    # 推送（任何异常静默吞掉，绝不影响主流程）
    if settings.feishu_webhook:
        try:
            from .notifier import send_feishu

            send_feishu(result, webhook=settings.feishu_webhook, keyword=settings.feishu_keyword)
        except Exception as exc:
            log.warning("notify.failed", error=str(exc))

    return result


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
