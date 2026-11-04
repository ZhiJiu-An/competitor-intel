"""查询服务：把只读数据装配成 agent 能直接用的结构。

不含 MCP 概念，也不做网络 IO（触发采集在 bridge.py）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from ...core import diff as diff_module
from ...core import models
from ...core import query as store
from ...core.config import Settings
from ...core.errors import CiError


def list_targets(settings: Settings) -> dict:
    if not settings.targets_path.exists():
        raise CiError(
            "TARGETS_NOT_FOUND",
            f"监控配置不存在: {settings.targets_path}",
            hint="设置 TARGETS_PATH 指向 config/targets.yaml（默认在 COMPETITOR_INTEL_HOME 下）",
        )

    raw = yaml.safe_load(settings.targets_path.read_text(encoding="utf-8")) or {}
    targets = [
        {
            "competitor": entry["competitor"],
            "page_type": page.get("type"),
            "url": page.get("url"),
            "main_selector": page.get("main_selector"),
        }
        for entry in raw.get("targets") or []
        for page in entry.get("pages") or []
    ]
    return {"defaults": raw.get("defaults") or {}, "count": len(targets), "targets": targets}


async def list_snapshots(
    settings: Settings,
    *,
    competitor: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> dict:
    items = await store.list_snapshots(
        settings, competitor=competitor, since=since, until=until, limit=limit
    )
    return {
        "filters": {"competitor": competitor, "since": since, "until": until, "limit": limit},
        "count": len(items),
        "items": items,
    }


async def get_snapshot(
    settings: Settings,
    *,
    competitor: str,
    page_url: str | None = None,
    as_of: str | None = None,
) -> dict:
    rows = await store.latest_extracted(
        settings, competitor=competitor, page_url=page_url, as_of=as_of
    )
    if not rows:
        suffix = f"（截至 {as_of}）" if as_of else ""
        raise CiError(
            "NOT_FOUND",
            f"没有 {competitor} 的已抽取快照{suffix}",
            hint="用 list_snapshots 确认库里有什么；数据太旧就先 trigger_run 再查",
        )

    items = [
        {
            "competitor": row["competitor"],
            "page_url": row["page_url"],
            "page_type": row["page_type"],
            "snapshot_date": row["snapshot_date"],
            "snapshot": json.loads(row["payload_json"]),
        }
        for row in rows
    ]
    return {"competitor": competitor, "count": len(items), "items": items}


async def _resolve_page(settings: Settings, competitor: str, page_url: str | None) -> str:
    if page_url:
        return page_url

    rows = await store.latest_extracted(settings, competitor=competitor)
    if not rows:
        raise CiError(
            "NOT_FOUND",
            f"没有 {competitor} 的已抽取快照",
            hint="先 trigger_run，或用 list_snapshots 查库里已有数据",
        )
    pages = [row["page_url"] for row in rows]
    if len(pages) > 1:
        raise CiError(
            "BAD_ARGUMENT",
            f"{competitor} 有多个页面，必须指定 page_url",
            hint=f"可选: {', '.join(pages)}",
        )
    return pages[0]


async def diff_snapshots(
    settings: Settings,
    *,
    competitor: str,
    page_url: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    page = await _resolve_page(settings, competitor, page_url)

    if to_date:
        # 「截至某天」而不是「必须是采集日」：传今天也能拿到最近一条。
        current = await store.latest_on_or_before(
            settings, competitor=competitor, page_url=page, day=to_date
        )
        if current is None:
            dates = await store.available_dates(
                settings, competitor=competitor, page_url=page
            )
            raise CiError(
                "NOT_FOUND",
                f"{competitor} {page} 在 {to_date} 及之前没有已抽取快照",
                hint=f"可用日期: {', '.join(dates) if dates else '（无）'}",
            )
    else:
        latest = await store.latest_extracted(
            settings, competitor=competitor, page_url=page
        )
        if not latest:
            raise CiError(
                "NOT_FOUND",
                f"没有 {competitor} {page} 的已抽取快照",
                hint="先 trigger_run，或换一个 competitor/page_url",
            )
        current = latest[0]

    if from_date:
        baseline = await store.latest_on_or_before(
            settings, competitor=competitor, page_url=page, day=from_date
        )
        if baseline is None:
            dates = await store.available_dates(
                settings, competitor=competitor, page_url=page
            )
            raise CiError(
                "NOT_FOUND",
                f"没有 {from_date} 及之前的快照可作基线",
                hint=f"可用日期: {', '.join(dates) if dates else '（无）'}",
            )
    else:
        baseline = await store.snapshot_before(
            settings,
            competitor=competitor,
            page_url=page,
            before=current["snapshot_date"],
        )

    if baseline is None:
        raise CiError(
            "NOT_FOUND",
            f"没有早于 {current['snapshot_date']} 的快照可作基线",
            hint="该页面只有一条快照，等下一次采集后再对比",
        )

    old = models.CompetitorSnapshot.model_validate_json(baseline["payload_json"])
    new = models.CompetitorSnapshot.model_validate_json(current["payload_json"])
    result = diff_module.diff_snapshots(old, new)
    return {**result.model_dump(), "changed": result.changed}


async def list_changes(
    settings: Settings,
    *,
    competitor: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_unchanged: bool = False,
) -> dict:
    """跨竞品/页面汇总窗口内的字段级变化：一次调用回答「最近改了什么」。"""
    current_rows = await store.latest_extracted(
        settings, competitor=competitor, as_of=until
    )
    if not current_rows:
        return {
            "since": since,
            "until": until,
            "count": 0,
            "items": [],
            "note": "库中没有可比较的已抽取快照",
        }

    items: list[dict] = []
    for current in current_rows:
        page = {"competitor": current["competitor"], "page_url": current["page_url"]}
        baseline = (
            await store.latest_on_or_before(settings, **page, day=since)
            if since
            else await store.snapshot_before(
                settings, **page, before=current["snapshot_date"]
            )
        )

        changes: list[dict] = []
        status = "unchanged"
        baseline_date = baseline["snapshot_date"] if baseline else None

        if baseline is None:
            status = "new"
        elif baseline["snapshot_date"] != current["snapshot_date"]:
            old = models.CompetitorSnapshot.model_validate_json(baseline["payload_json"])
            new = models.CompetitorSnapshot.model_validate_json(current["payload_json"])
            result = diff_module.diff_snapshots(old, new)
            changes = [change.model_dump() for change in result.changes]
            status = "changed" if result.changed else "unchanged"

        if status != "unchanged" or include_unchanged:
            items.append(
                {
                    "competitor": current["competitor"],
                    "page_type": current["page_type"],
                    "page_url": current["page_url"],
                    "baseline_date": baseline_date,
                    "current_date": current["snapshot_date"],
                    "status": status,
                    "changes": changes,
                }
            )

    items.sort(key=lambda item: (item["competitor"], item["page_url"]))
    return {
        "since": since,
        "until": until,
        "count": len(items),
        "items": items,
        "note": "默认只列变化/新增页面；status=new 表示窗口起点前没有基线",
    }


async def get_review_queue(settings: Settings) -> dict:
    items = await store.review_queue(settings)
    return {
        "count": len(items),
        "items": items,
        "note": "抽取失败不会静默消失；空列表才代表没有待复核项",
    }


def report_link(settings: Settings, report_path: str | None) -> dict | None:
    """把库里的报告路径翻成可直接打开的链接（与飞书卡片同源）。"""
    if not report_path:
        return None
    path = Path(report_path)
    if not path.is_absolute():
        path = settings.home / path
    try:
        name = path.resolve().relative_to(settings.report_dir.resolve()).as_posix()
    except ValueError:
        name = path.name

    link = {"path": str(path), "name": name}
    if settings.report_base_url:
        link["url"] = f"{settings.report_base_url.rstrip('/')}/reports/{name}"
    return link


async def run_digest(settings: Settings, run: dict) -> dict:
    """把一次 run 的终态整理成「这次抓到了什么」：页面 + 变化 + 报告链接。

    run 记录本身只有计数，没有明细；明细来自当天的快照，
    因此按 snapshot_date == 运行日过滤，避免把别的日子的数据混进来。
    """
    day = (run.get("finished_at") or run.get("created_at") or "")[:10]
    day = day or date.today().isoformat()

    changes = await list_changes(
        settings, competitor=run.get("competitor"), until=day, include_unchanged=True
    )
    pages = [item for item in changes["items"] if item["current_date"] == day]
    changed = [item for item in pages if item["changes"]]

    return {
        "run": {
            "run_id": run.get("id"),
            "status": run.get("status"),
            "competitor": run.get("competitor"),
            "created_at": run.get("created_at"),
            "finished_at": run.get("finished_at"),
            "counts": run.get("counts") or {},
            "error_summary": run.get("error_summary"),
        },
        "pages": pages,
        "changed": changed,
        "report": report_link(settings, run.get("report_path")),
        "note": (
            "pages 是本次运行日有快照的页面（含未变化）；"
            "changed 只含字段级变化。完整数值用 get_snapshot 查。"
        ),
    }


async def get_report(settings: Settings) -> dict:
    candidates: list[Path] = []

    run = await store.latest_run(settings)
    if run and run.get("report_path"):
        path = Path(run["report_path"])
        candidates.append(path if path.is_absolute() else settings.home / path)

    if settings.report_dir.is_dir():
        candidates += sorted(
            settings.report_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            return {"path": str(path), "chars": len(text), "markdown": text}

    raise CiError(
        "REPORT_NOT_FOUND",
        f"还没有生成过报告（目录: {settings.report_dir}）",
        hint="先 trigger_run 跑一次采集，或在仓库根执行 `ci report`",
    )
