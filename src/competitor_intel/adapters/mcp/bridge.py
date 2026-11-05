"""进程内桥接：MCP 工具 -> 核心编排。

不再经 HTTP：MCP 进程直接读 SQLite、直接跑采集。
只做三件事：参数兜底、超时、把异常翻成带 hint 的 CiError。
"""

from __future__ import annotations

import asyncio
import json
import logging

from ...core import orchestrator
from ...core import query as store
from ...core.config import Settings
from ...core.errors import CiError
from ...core.netguard import ensure_allowed

logger = logging.getLogger("competitor_intel.mcp.bridge")

TERMINAL = {"succeeded", "partial", "failed"}

#: 本进程内触发的采集任务：run_id -> Task，用于 wait_for_run 直接 await
_TASKS: dict[str, asyncio.Task] = {}


def _normalize(record: dict) -> dict:
    if "counts_json" in record:
        record = dict(record)
        record["counts"] = json.loads(record.pop("counts_json") or "{}")
    return record


async def get_run(settings: Settings, run_id: str) -> dict:
    record = await store.get_run(settings, run_id)
    if record is None:
        raise CiError(
            "NOT_FOUND",
            f"没有 run_id={run_id} 的运行记录",
            hint="run_id 来自 trigger_run 的返回；不确定就重新 trigger_run（同日幂等）",
        )
    return _normalize(record)


async def _execute(
    settings: Settings,
    run_id: str,
    *,
    competitor: str | None,
    force: bool,
) -> None:
    try:
        await orchestrator.execute_run(
            settings, run_id=run_id, competitor=competitor, force=force
        )
    except Exception:  # 失败已写入 run 记录，这里只留日志
        logger.exception("mcp run %s failed", run_id)


async def trigger_run(
    settings: Settings,
    *,
    competitor: str | None = None,
    force: bool = False,
) -> dict:
    """创建（或复用）当日 run；新建时在本进程后台执行，供 wait_for_run 等待。"""
    record, created = await orchestrator.enqueue_run(
        settings, competitor=competitor, force=force
    )
    run_id = record["id"]
    if created and run_id not in _TASKS:
        task = asyncio.create_task(
            _execute(settings, run_id, competitor=competitor, force=force)
        )
        _TASKS[run_id] = task

        def _forget(_task: asyncio.Task, key: str = run_id) -> None:
            _TASKS.pop(key, None)

        task.add_done_callback(_forget)
    return {
        "run_id": run_id,
        "status": record["status"],
        "idempotent": not created,
    }


async def wait_for_run(
    settings: Settings, run_id: str, *, timeout_s: float | None = None
) -> dict:
    """等待 run 进入终态；超时抛 TIMEOUT 并带上 run_id，绝不重复触发。"""
    timeout = settings.run_timeout_s if timeout_s is None else timeout_s
    task = _TASKS.get(run_id)
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError as exc:
            raise CiError(
                "TIMEOUT",
                f"{timeout:.0f}s 内未跑完采集",
                hint=f"用 get_run 查询 run_id={run_id}，不要重新触发",
                retryable=True,
            ) from exc
        except Exception:
            pass  # 失败原因已落在 run 记录里，下面统一读库返回

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        record = await store.get_run(settings, run_id)
        if record is None:
            raise CiError(
                "NOT_FOUND",
                f"没有 run_id={run_id} 的运行记录",
                hint="确认 run_id 来自 trigger_run 的返回",
            )
        if record["status"] in TERMINAL:
            return _normalize(record)
        if loop.time() >= deadline:
            raise CiError(
                "TIMEOUT",
                f"{timeout:.0f}s 内未跑完采集",
                hint=f"用 get_run 查询 run_id={run_id}，不要重新触发",
                retryable=True,
            )
        await asyncio.sleep(1)


def _check_url(url: str, settings: Settings) -> None:
    """http/https 与内网限制统一由 core.netguard 判定。"""
    ensure_allowed(url, allow_private=settings.allow_private_networks)


async def crawl_page(
    settings: Settings,
    *,
    url: str,
    competitor: str | None = None,
    page_type: str = "pricing",
    main_selector: str | None = None,
    force: bool = False,
) -> dict:
    _check_url(url, settings)
    try:
        result = await asyncio.wait_for(
            orchestrator.crawl_page(
                settings,
                url=url,
                competitor=competitor,
                page_type=page_type,
                main_selector=main_selector,
                force=force,
            ),
            timeout=settings.crawl_timeout_s,
        )
    except TimeoutError as exc:
        raise CiError(
            "TIMEOUT",
            f"{settings.crawl_timeout_s:.0f}s 内未抓完 {url}",
            hint="页面可能过重或需要登录；稍后重试或换一个 URL",
            retryable=True,
        ) from exc
    except CiError:
        raise
    except Exception as exc:
        raise CiError(
            "EXTRACTION_ERROR",
            f"抓取失败: {exc}",
            hint="检查 URL 是否可公开访问；详情见服务端日志",
            retryable=True,
        ) from exc

    if result.get("status") == "blocked":
        raise CiError(
            "BLOCKED",
            f"robots.txt 不允许抓取 {url}",
            hint="换一个允许抓取的页面，或联系站点方获取授权",
        )
    if result.get("status") == "extraction_error":
        raise CiError(
            "EXTRACTION_ERROR",
            f"页面抽取失败: {result.get('error_detail') or '未知原因'}",
            hint="页面结构可能已变；可用 list_review_queue 查看失败记录",
        )
    return result
