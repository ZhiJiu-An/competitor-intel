"""加载 targets.yaml 与环境变量并校验（TDD 4.2 / 8.1 / 8.2）。

对外契约：load_config(path) -> Config；iter_targets(config) -> list[Target]
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Defaults(BaseModel):
    request_interval_s: int = 2  # 合规：请求间隔 >= 2s
    respect_robots: bool = True
    timeout_s: int = 30
    cache_mode: str = "bypass"  # Crawl4AI 缓存无 TTL，默认绕开；增量交给业务指纹


class PageEntry(BaseModel):
    type: str = Field(description="pricing / changelog")
    url: str
    main_selector: str = Field(description="指纹容器 CSS 选择器")
    pricing_selector: str | None = None


class CompetitorEntry(BaseModel):
    competitor: str
    pages: list[PageEntry]


class Config(BaseModel):
    defaults: Defaults = Defaults()
    targets: list[CompetitorEntry]


class Target(BaseModel):
    """扁平化后的单页采集目标，是图节点处理的基本单位。"""

    competitor: str
    page_type: str
    url: str
    main_selector: str
    pricing_selector: str | None = None


class Settings(BaseModel):
    """环境变量（TDD 8.2）。

    llm_api_key 默认空串：init-db / report 等无需 LLM 的命令可直接运行；
    采集路径在 _build_strategy 处对空 key 报错。
    """

    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_model: str = "deepseek-chat"
    db_path: str = "data/snapshots.db"
    report_dir: str = "data/reports"
    log_level: str = "INFO"
    api_token: str = ""  # 留空则不鉴权（默认仅监听 127.0.0.1）
    feishu_webhook: str = ""  # 留空则不推送
    feishu_keyword: str = "竟品分析"  # webhook 关键词校验时用
    report_base_url: str = "http://127.0.0.1:8000"  # 卡片里报告按钮的地址前缀
    # 浏览器通道：默认用 Playwright 自带的 chromium；本机没装浏览器时
    # 设 CRAWLER_BROWSER_CHANNEL=chrome 直接复用已安装的 Chrome。
    browser_channel: str = "chromium"

    @classmethod
    def from_env(cls) -> "Settings":
        # 项目根 .env 作为默认值；已存在的环境变量优先（override=False）
        if _ENV_FILE.exists():
            load_dotenv(_ENV_FILE, override=False)
        return cls(
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL"),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
            db_path=os.getenv("DB_PATH", "data/snapshots.db"),
            report_dir=os.getenv("REPORT_DIR", "data/reports"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            api_token=os.getenv("API_TOKEN", ""),
            feishu_webhook=os.getenv("FEISHU_WEBHOOK", ""),
            feishu_keyword=os.getenv("FEISHU_KEYWORD", "竞品分析"),
            report_base_url=os.getenv("REPORT_BASE_URL", "http://127.0.0.1:8000"),
            browser_channel=os.getenv("CRAWLER_BROWSER_CHANNEL", "chromium"),
        )


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


def iter_targets(config: Config) -> list[Target]:
    return [
        Target(
            competitor=entry.competitor,
            page_type=page.type,
            url=page.url,
            main_selector=page.main_selector,
            pricing_selector=page.pricing_selector,
        )
        for entry in config.targets
        for page in entry.pages
    ]
