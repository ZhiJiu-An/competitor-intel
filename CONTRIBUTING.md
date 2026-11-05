# 贡献指南

## 开发环境

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
playwright install chromium    # 或 export CRAWLER_BROWSER_CHANNEL=chrome
pytest -q
```

## 架构边界（三条硬规则）

### 1. `core` 不得 import `adapters`

```
core/ → 只依赖标准库与第三方库
adapters/ → 可以 import core
cli.py → 可以 import core 与 adapters
```

原因：内核必须能独立安装运行（`pip install -e .` 不装 MCP / FastAPI / Celery）。任何“通知、协议、调度”的副作用都通过参数注入，例如飞书推送：

```python
# core/orchestrator.py
async def execute_run(..., notify: Notifier | None = None) -> dict: ...

# adapters/schedule/tasks.py
await execute_run(..., notify=build_notifier(settings))
```

违反这条会让“只要雷达”的用户被迫装上整套服务端依赖。

### 2. stdout 只留协议帧

MCP 走 stdio：**stdout 是协议通道**。所有日志与调试输出必须走 stderr（`structlog` 默认即 stderr，`print` 不得出现在 `adapters/mcp` 与 `core` 的导入链上）。

新增工具或中间件时，用 `log.info(...)` 而不是 `print(...)`。

### 3. 可恢复错误走工具错误通道

MCP 工具不要抛裸异常或返回空列表。统一抛 `core.errors.CiError(code, message, hint, retryable)`：

- `code` 机器可读，客户端据此决定重试还是追问用户；
- `hint` 给模型下一步动作（例如超时后带 `run_id`，说明“不要重复触发”）；
- 只有真正的程序缺陷才让异常冒到协议层。

## 数据与迁移

`core/store.py` 的 DDL 变更必须同时提供**旧库迁移**，参考 `origin` 列的实现：建表后用 `PRAGMA table_info` 检测缺列并 `ALTER TABLE`。不要要求用户删库重建。

## 测试约定

- 单测不触网、不花 token：LLM 用 monkeypatch，页面用 `tests/fixtures`。
- 变更采集语义（指纹、diff、状态机、origin）时，必须补对应测试。
- 涉及出站地址的测试用 `netguard` 的私有开关或 monkeypatch，不要依赖真实 DNS。
- 提交前跑：

```bash
pytest -q
ruff check .
mypy src
python scripts/smoke_e2e.py     # 改了采集 / 报告链路时
```

CI（`.github/workflows/ci.yml`）跑的是同一组门禁：`ruff` + `mypy` + `pytest`。

## 提交信息

用一句话说明“为什么改”，例如：

```
fix: adhoc 抓取不再覆盖当日 scheduled 快照
feat: MCP crawl_page 支持 main_selector 覆盖
```

## 发布形态

本项目面向**本地部署型开源**：不假设托管服务、不引入账号体系。新增依赖时请说明它属于哪个 extra（`mcp` / `http` / `schedule` / `dev`），保持“按需下载”的能力边界。
