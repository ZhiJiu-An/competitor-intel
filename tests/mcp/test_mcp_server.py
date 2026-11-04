"""协议层契约：工具面、只读声明、错误翻译、resource。"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from competitor_intel.adapters.mcp import server


async def _tools() -> dict:
    return {tool.name: tool for tool in await server.mcp.list_tools()}


def _call(name: str, arguments: dict | None = None):
    return server.mcp.call_tool(name, arguments or {})


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


def _error_body(error: Exception) -> dict:
    text = str(error)
    return json.loads(text[text.index("{") :])


async def test_tool_surface_is_small_and_explicit():
    assert set(await _tools()) == {
        "list_targets",
        "list_snapshots",
        "list_changes",
        "get_snapshot",
        "diff_snapshots",
        "list_review_queue",
        "get_report",
        "crawl_page",
        "trigger_run",
        "get_run",
    }


async def test_read_tools_are_declared_read_only():
    writing = {"trigger_run", "crawl_page"}
    for name, tool in (await _tools()).items():
        assert tool.description
        assert tool.annotations is not None, f"{name} 缺少 annotations"
        expected = name not in writing
        assert tool.annotations.read_only_hint is expected, name


async def test_trigger_is_marked_as_writing_and_open_world():
    tools = await _tools()
    for name in ("trigger_run", "crawl_page"):
        annotations = tools[name].annotations
        assert annotations.read_only_hint is False
        assert annotations.open_world_hint is True


def test_instructions_warn_about_untrusted_content():
    assert "list_changes" in server.INSTRUCTIONS
    assert "snapshot_date" in server.INSTRUCTIONS
    assert "不可信" in server.INSTRUCTIONS
    assert "trigger_run" in server.INSTRUCTIONS


async def test_get_snapshot_tool_returns_structured_content(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    payload = _payload(await _call("get_snapshot", {"competitor": "acme"}))

    assert payload["items"][0]["snapshot"]["pricing_tiers"][0]["name"] == "Pro"


async def test_recoverable_failure_becomes_structured_tool_error(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    with pytest.raises(ToolError) as excinfo:
        await _call("diff_snapshots", {"competitor": "nobody"})

    body = _error_body(excinfo.value)
    assert body["code"] == "NOT_FOUND"
    assert body["hint"]


async def test_crawl_page_rejects_non_http_url(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    with pytest.raises(ToolError) as excinfo:
        await _call("crawl_page", {"url": "ftp://example.test/pricing"})

    body = _error_body(excinfo.value)
    assert body["code"] == "BAD_ARGUMENT"
    assert "http" in body["hint"]


async def test_trigger_run_calls_core_in_process_and_returns_digest(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    calls: list[tuple] = []

    async def fake_trigger(current, *, competitor=None, force=False):
        calls.append(("trigger", competitor, force))
        return {"run_id": "r1", "status": "queued", "idempotent": True}

    async def fake_wait(current, run_id, *, timeout_s=None):
        calls.append(("wait", run_id, timeout_s))
        return {
            "id": run_id,
            "status": "succeeded",
            "competitor": None,
            "counts": {},
            "finished_at": "2026-10-27T09:00:00",
            "report_path": None,
        }

    monkeypatch.setattr(server.client, "trigger_run", fake_trigger)
    monkeypatch.setattr(server.client, "wait_for_run", fake_wait)

    payload = _payload(await _call("trigger_run", {}))
    assert calls == [("trigger", None, False), ("wait", "r1", None)]
    assert payload["idempotent"] is True
    assert payload["run"]["run_id"] == "r1"
    assert payload["run"]["status"] == "succeeded"


async def test_run_digest_returns_pages_changes_and_report(settings):
    (settings.report_dir / "2026-10-27.md").write_text("# 报告", encoding="utf-8")
    run = {
        "id": "r1",
        "status": "succeeded",
        "competitor": None,
        "counts": {"processed": 2, "extracted": 2},
        "finished_at": "2026-10-27T09:00:00",
        "report_path": str(settings.report_dir / "2026-10-27.md"),
    }

    digest = await server.service.run_digest(settings, run)

    assert digest["run"]["run_id"] == "r1"
    assert {item["competitor"] for item in digest["pages"]} == {"acme", "beta"}
    assert {item["competitor"] for item in digest["changed"]} == {"acme", "beta"}
    assert digest["changed"][0]["changes"][0]["field"] == "pricing_tiers[Pro].amount"
    assert digest["report"]["url"] == "http://127.0.0.1:8000/reports/2026-10-27.md"


async def test_latest_report_resource(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    (settings.report_dir / "latest.md").write_text("# 竞品报告", encoding="utf-8")

    content = await server.mcp.read_resource("ci://reports/latest")
    text = "".join(block.content for block in content)

    assert "竞品报告" in text


async def test_missing_report_raises_resource_error(settings, monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    with pytest.raises(ResourceError):
        await server.mcp.read_resource("ci://reports/latest")
