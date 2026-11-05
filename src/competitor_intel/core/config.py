"""加载 targets.yaml 与环境变量并校验（TDD 4.2 / 8.1 / 8.2）。

对外契约：load_config(path) -> Config；iter_targets(config) -> list[Target]；
Settings.from_env() -> 统一配置（引擎、MCP、定时任务共用）。

home 解析链（决定 data/、config/、.env 的落点）：
1. `COMPETITOR_INTEL_HOME`
2. 从当前目录向上查找含 `config/targets.yaml` 的目录
3. 源码仓库根（src 布局）
4. `~/.competitor-intel`
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


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

    @field_validator("url")
    @classmethod
    def _only_http(cls, value: str) -> str:
        # 配置里的 URL 直接喂给浏览器，file:// / data: 之类不能进
        if not value.startswith(("http://", "https://")):
            raise ValueError("只支持 http/https URL")
        return value


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


def _default_home() -> Path:
    env = os.getenv("COMPETITOR_INTEL_HOME")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "config" / "targets.yaml").exists():
            return candidate
    repo = Path(__file__).resolve().parents[3]
    if (repo / "config" / "targets.yaml").exists():
        return repo
    return Path.home() / ".competitor-intel"


class Settings(BaseModel):
    """统一配置：引擎、MCP、定时任务读同一份环境变量。

    llm_api_key 默认空串：init-db / report 等无需 LLM 的命令可直接运行；
    采集路径在 _build_strategy 处对空 key 报错。
    """

    home: Path = Field(default_factory=_default_home)
    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_model: str = "deepseek-chat"
    db_path: Path = Path("data/snapshots.db")
    targets_path: Path = Path("config/targets.yaml")
    report_dir: Path = Path("data/reports")
    log_level: str = "INFO"
    api_token: str = ""  # 留空则不鉴权（默认仅监听 127.0.0.1）
    feishu_webhook: str = ""  # 留空则不推送
    feishu_keyword: str = "竞品分析"  # webhook 关键词校验时用
    #: 推送策略：changes=有字段级变化才推；always=每次定时 run 都推
    feishu_notify_mode: Literal["changes", "always"] = "changes"
    report_base_url: str = "http://127.0.0.1:8000"  # 卡片里报告按钮的地址前缀
    # 浏览器通道：默认用 Playwright 自带的 chromium；本机没装浏览器时
    # 设 CRAWLER_BROWSER_CHANNEL=chrome 直接复用已安装的 Chrome。
    browser_channel: str = "chromium"
    #: 只读查询的单次超时（秒），MCP 工具内部使用
    timeout_s: float = 20.0
    #: 同步抓取一页要跑浏览器 + LLM，20s 不够，单独放宽
    crawl_timeout_s: float = 240.0
    #: 触发批量采集后等待结果的上限
    run_timeout_s: float = 240.0
    #: 是否允许抓取回环/内网地址（默认拒绝，防 SSRF）
    allow_private_networks: bool = False

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @classmethod
    def from_env(cls) -> "Settings":
        home = _default_home()
        env_file = home / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)

        def _path(name: str, default: str) -> Path:
            raw = Path(os.getenv(name, default)).expanduser()
            return raw if raw.is_absolute() else home / raw

        raw_mode = os.getenv("FEISHU_NOTIFY_MODE", "changes").strip().lower()
        notify_mode: Literal["changes", "always"] = (
            "always" if raw_mode == "always" else "changes"
        )

        return cls(
            home=home,
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv("LLM_BASE_URL"),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
            db_path=_path("DB_PATH", "data/snapshots.db"),
            targets_path=_path("TARGETS_PATH", "config/targets.yaml"),
            report_dir=_path("REPORT_DIR", "data/reports"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            api_token=os.getenv("API_TOKEN", ""),
            feishu_webhook=os.getenv("FEISHU_WEBHOOK", ""),
            feishu_keyword=os.getenv("FEISHU_KEYWORD", "竞品分析"),
            feishu_notify_mode=notify_mode,
            report_base_url=os.getenv("REPORT_BASE_URL", "http://127.0.0.1:8000"),
            browser_channel=os.getenv("CRAWLER_BROWSER_CHANNEL", "chromium"),
            timeout_s=float(os.getenv("CI_API_TIMEOUT_S", "20")),
            crawl_timeout_s=float(os.getenv("CI_CRAWL_TIMEOUT_S", "240")),
            run_timeout_s=float(os.getenv("CI_RUN_TIMEOUT_S", "240")),
            allow_private_networks=os.getenv("ALLOW_PRIVATE_NETWORKS", "").lower()
            in ("1", "true", "yes"),
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
