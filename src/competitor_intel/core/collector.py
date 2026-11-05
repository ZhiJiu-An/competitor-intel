"""采集层：Crawl4AI 主路径 + Playwright 兜底，统一返回 FetchResult（TDD 4.2 / 6.2）。

数据流（与 TDD 图 3 一致）：
    robots 检查 -> 限速 -> Crawl4AI 抓取 -> 指纹短路 -> LLM 抽取 -> 三段校验 -> 落库
    抓取异常 -> Playwright 直取 HTML（最多 1 次）-> 复用同一套抽取与校验

对外契约：
- fetch_and_extract(target, *, force=False) -> FetchResult（副作用：网络请求、写库）
- playwright_fallback(target) -> FetchResult（副作用：网络请求）
"""

import asyncio
import inspect
import json
import os
import time
import urllib.request
import urllib.robotparser
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import structlog
from pydantic import BaseModel, ValidationError

from .config import Defaults, Settings, Target
from .fingerprint import compute_fingerprint
from .models import CompetitorSnapshot
from .store import latest_hash, save_snapshot

log = structlog.get_logger(__name__)

FetchStatus = Literal["extracted", "extraction_error", "skipped", "blocked"]


class FetchResult(BaseModel):
    target: Target
    status: FetchStatus
    content_hash: str | None = None
    snapshot: CompetitorSnapshot | None = None
    error_detail: str | None = None
    used_fallback: bool = False


class BusinessAssertionError(ValueError):
    """业务层断言失败（三段校验之第三段）。"""


# ---------------------------------------------------------------- 合规与限速

_last_request_at = 0.0
_throttle_lock = asyncio.Lock()


async def _throttle(interval_s: int) -> None:
    """保证两次请求间隔 >= interval_s（P5 合规内置）。"""
    global _last_request_at
    if interval_s <= 0:
        return
    async with _throttle_lock:
        wait = _last_request_at + interval_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


async def _allowed_by_robots(url: str, defaults: Defaults) -> bool:
    if not defaults.respect_robots:
        return True
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    def _check() -> bool:
        try:
            with urllib.request.urlopen(robots_url, timeout=10) as resp:
                text = resp.read().decode("utf-8", "ignore")
        except Exception:
            # 无法获取 robots.txt 按惯例视为允许；站点不可达会在抓取阶段暴露
            return True
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        return rp.can_fetch("*", url)

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------- 抓取（网络层）


async def _fetch_html(target: Target, settings: Settings, defaults: Defaults) -> str:
    """Crawl4AI 主路径抓取，返回 cleaned HTML。只做抓取，不做 LLM 抽取——
    抽取发生在指纹判定之后，这是第二层成本控制的前提。"""
    # Crawl4AI 默认把缓存写在 ~/.crawl4ai；HOME 只读时其 RobotsParser 初始化会抛
    # "attempt to write a readonly database" 并让整条主路径失败。
    # 重定向到与业务库同级的可写数据目录（容器内即挂载卷），必须在导入前设置。
    os.environ.setdefault(
        "CRAWL4_AI_BASE_DIRECTORY", str(Path(settings.db_path).resolve().parent)
    )
    from crawl4ai import AsyncWebCrawler  # 延迟导入，CLI 非抓取命令不付启动成本

    kwargs: dict = {"url": target.url}
    try:
        from crawl4ai import BrowserConfig, CacheMode, CrawlerRunConfig

        # Crawl4AI 实际读取的是 chrome_channel（channel 只用于 cdp 场景）
        browser_cfg = BrowserConfig(headless=True, chrome_channel=settings.browser_channel)
        run_cfg = CrawlerRunConfig(
            cache_mode=(
                CacheMode.ENABLED if defaults.cache_mode == "enabled" else CacheMode.BYPASS
            ),
            page_timeout=defaults.timeout_s * 1000,
        )
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(config=run_cfg, **kwargs)
    except ImportError:  # 旧版 0.4.x API
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                bypass_cache=defaults.cache_mode != "enabled", **kwargs
            )

    if not getattr(result, "success", False):
        raise RuntimeError(f"crawl4ai 抓取失败: {getattr(result, 'error_message', 'unknown')}")
    html = getattr(result, "cleaned_html", None) or getattr(result, "html", None) or ""
    if not html.strip():
        raise RuntimeError("crawl4ai 返回空页面")
    return html


async def _playwright_html(target: Target, defaults: Defaults, settings: Settings) -> str:
    """Playwright 兜底：只直取 page.content()，不承担抽取。

    浏览器通道与主路径一致（settings.browser_channel）：本机没装 Playwright 自带
    chromium 时用 chrome 复用系统已安装的浏览器。
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        launch_kwargs: dict = {"headless": True}
        if settings.browser_channel and settings.browser_channel != "chromium":
            launch_kwargs["channel"] = settings.browser_channel
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            page = await browser.new_page()
            await page.goto(
                target.url, wait_until="domcontentloaded", timeout=defaults.timeout_s * 1000
            )
            html = await page.content()
        finally:
            await browser.close()
    if not html.strip():
        raise RuntimeError("playwright 返回空页面")
    return html


# ---------------------------------------------------------------- LLM 抽取


def _build_strategy(settings: Settings):
    """LLMExtractionStrategy：schema 即契约，Field(description) 即抽取指令。"""
    if not settings.llm_api_key:
        raise RuntimeError("缺少 LLM_API_KEY，无法执行抽取")
    try:
        from crawl4ai import LLMExtractionStrategy
    except ImportError:
        from crawl4ai.extraction_strategy import LLMExtractionStrategy

    from .models import CompetitorSnapshot as _Snap

    instruction = (
        "从页面内容中抽取竞品定价与功能信息，严格按给定 JSON Schema 输出。"
        "价格只填数值，免费或面议填 null；币种用 ISO 代码；"
        "功能项拆成名称与一句话描述；target_user_note 从页面原文归纳目标用户。"
        "套餐制页面（Free/Pro/Enterprise、按月或按年订阅）用 amount + billing_period，"
        "components 留空；按量计费页面（按 token、调用次数、席位计价）把每个计费维度"
        "放进 components：unit 只写计量单位（如 1M tokens、席位/月、千次调用），"
        "amount 写数值，currency 写币种，condition 用简短短语标注维度"
        "（如 输入·缓存未命中·高峰、输出·错峰、年付），"
        "绝不要复制页面的脚注长句或时段说明，长说明放 key_limits；"
        "此时 amount 留 null，且同一计费对象（如同一模型）的所有维度归入同一个 tier，"
        "不要把价格写进 key_limits。pricing_model 填 subscription / usage_based / one_time / custom。"
    )
    kwargs: dict = {
        "provider": (
            settings.llm_model if "/" in settings.llm_model else f"openai/{settings.llm_model}"
        ),
        "api_token": settings.llm_api_key,
        "schema": _Snap.model_json_schema(),
        "extraction_type": "schema",
        "instruction": instruction,
        "input_format": "html",
    }
    sig = inspect.signature(LLMExtractionStrategy.__init__)
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_kwargs:
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        # 透传 **kwargs：__init__ 里的 input_format / base_url 都在 kwargs 中消费
        return LLMExtractionStrategy(**kwargs)
    if settings.llm_base_url and "base_url" in sig.parameters:
        kwargs["base_url"] = settings.llm_base_url
    return LLMExtractionStrategy(**{k: v for k, v in kwargs.items() if k in sig.parameters})


def _parse_extracted(raw: object) -> dict:
    """归一化 LLMExtractionStrategy 的输出（可能是分块列表）。"""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        merged: dict = {}
        for block in data:
            content = block.get("content", block) if isinstance(block, dict) else block
            if isinstance(content, dict):
                merged.update({k: v for k, v in content.items() if v not in (None, [], "")})
        return merged
    return {}


async def _extract(html: str, url: str, settings: Settings) -> dict:
    """主路径与兜底共用的同一套抽取策略。"""
    strategy = _build_strategy(settings)
    raw = await asyncio.to_thread(strategy.run, url, [html])
    return _parse_extracted(raw)


# ---------------------------------------------------------------- 三段校验


def _business_assertions(snap: CompetitorSnapshot) -> list[str]:
    errors: list[str] = []
    if snap.page_type == "pricing" and not snap.pricing_tiers:
        errors.append("pricing 页至少抽取 1 个套餐")
    for tier in snap.pricing_tiers:
        if tier.amount is not None and not tier.currency:
            errors.append(f"套餐 {tier.name} 有价格但缺币种")
    return errors


def _validate(payload: dict, target: Target) -> CompetitorSnapshot:
    """类型层（Pydantic）+ 业务层断言；身份字段由系统确定，不交给 LLM。"""
    if not payload:
        raise BusinessAssertionError("LLM 抽取结果为空")
    data = dict(payload)
    data.update(
        competitor_name=target.competitor,
        page_url=target.url,
        snapshot_date=date.today().isoformat(),
        page_type=target.page_type,
    )
    snap = CompetitorSnapshot.model_validate(data)
    errors = _business_assertions(snap)
    if errors:
        raise BusinessAssertionError("; ".join(errors))
    return snap


# ---------------------------------------------------------------- 主流程


async def _process_html(
    target: Target,
    html: str,
    *,
    force: bool,
    settings: Settings,
    origin: str = "scheduled",
) -> FetchResult:
    """指纹短路 -> 抽取 -> 校验（纯流程，不写库）。"""
    content_hash = compute_fingerprint(html, target.main_selector)

    if not force:
        # 定时路径只跟定时快照比：adhoc 抓过同一页面不能顶替当天的定时采集；
        # adhoc 自己仍可复用任意 origin 的指纹，省下重复抽取成本。
        last = await latest_hash(
            settings.db_path,
            target.competitor,
            target.url,
            origin="scheduled" if origin == "scheduled" else None,
        )
        if last is not None and last == content_hash:
            log.info("fetch.skipped", url=target.url, competitor=target.competitor)
            return FetchResult(target=target, status="skipped", content_hash=content_hash)

    payload = await _extract(html, target.url, settings)
    try:
        snapshot = _validate(payload, target)
    except (ValidationError, BusinessAssertionError) as exc:
        log.warning(
            "fetch.extraction_error", url=target.url, competitor=target.competitor, error=str(exc)
        )
        return FetchResult(
            target=target,
            status="extraction_error",
            content_hash=content_hash,
            error_detail=str(exc),
        )
    log.info("fetch.extracted", url=target.url, competitor=target.competitor)
    return FetchResult(
        target=target, status="extracted", content_hash=content_hash, snapshot=snapshot
    )


async def playwright_fallback(
    target: Target,
    *,
    force: bool = False,
    settings: Settings | None = None,
    defaults: Defaults | None = None,
    origin: str = "scheduled",
) -> FetchResult:
    """兜底路径：Playwright 直取 HTML 后复用同一套抽取策略（最多触发 1 次）。

    robots.txt 禁止时不兜底，记 blocked。
    """
    settings = settings or Settings.from_env()
    defaults = defaults or Defaults()
    if not await _allowed_by_robots(target.url, defaults):
        return FetchResult(target=target, status="blocked")
    await _throttle(defaults.request_interval_s)
    html = await _playwright_html(target, defaults, settings)
    result = await _process_html(
        target, html, force=force, settings=settings, origin=origin
    )
    return result.model_copy(update={"used_fallback": True})


async def fetch_and_extract(
    target: Target,
    *,
    force: bool = False,
    settings: Settings | None = None,
    defaults: Defaults | None = None,
    origin: str = "scheduled",
) -> FetchResult:
    """主路径：robots -> 限速 -> Crawl4AI -> 指纹短路 -> 抽取校验 -> 落库。

    origin 区分 scheduled（定时/日报）与 adhoc（临时看一眼）：
    同一天同一页面，scheduled 覆盖 adhoc，adhoc 不覆盖 scheduled。
    """
    settings = settings or Settings.from_env()
    defaults = defaults or Defaults()

    if not await _allowed_by_robots(target.url, defaults):
        log.warning("fetch.blocked", url=target.url, competitor=target.competitor)
        return FetchResult(target=target, status="blocked")

    await _throttle(defaults.request_interval_s)
    try:
        html = await _fetch_html(target, settings, defaults)
        result = await _process_html(
            target, html, force=force, settings=settings, origin=origin
        )
    except Exception as exc:
        log.warning("fetch.fallback", url=target.url, error=str(exc), exc_info=True)
        result = await playwright_fallback(
            target, force=force, settings=settings, defaults=defaults, origin=origin
        )

    # 落库：blocked 不入库（状态机只含 extracted | extraction_error | skipped）
    if result.status != "blocked":
        await save_snapshot(
            settings.db_path,
            result.target,
            result.snapshot,
            content_hash=result.content_hash or "",
            status=result.status,
            error_detail=result.error_detail,
            origin=origin,
        )
    return result
