# competitor-intel

**Schema 契约驱动的竞品情报系统**：抓取竞品公开的定价页 / 更新日志，用 LLM 抽取成**可校验的结构化对象**，与历史快照做字段级 diff，产出可审计的 Markdown 报告。

同一套内核提供两种用法：

| 形态 | 解决的问题 | 入口 |
| --- | --- | --- |
| **雷达** | 每天自动盯住配置好的竞品页面，有变化才提醒 | `ci run` / Celery 定时 / `trigger_run` |
| **眼睛** | AI 随时抓一个用户甩来的 URL，当场拿到结构化数据 | `crawl_page`（MCP） |

> **Crawl4AI 抽、Pydantic 卡、SQLite 存、LangGraph 编、Playwright 兜、Markdown 报。**

---

## 为什么不是"爬虫 + AI 总结"

| 常见做法的问题 | 本项目的做法 |
| --- | --- |
| 输出是自由文本，无法比对、无法回溯 | 输出是 Pydantic 模型，字段级 diff 精确到 `pricing_tiers[Pro].amount` |
| 抽取失败被静默丢弃或错误填充 | 失败落库为 `extraction_error` 进复核队列，**永不消失** |
| 每次运行全量调 LLM，成本随竞品数线性涨 | 内容指纹未变 → 整条 LLM 链路短路，**零消耗** |

核心判断：**出错的数据比假装没出错的数据更危险。**

## 特性

- **契约即指令** —— Pydantic `Field(description)` 同时是抽取 prompt 和校验规则，改 Schema 即改行为
- **增量成本控制** —— 目标容器文本归一化后 sha256 指纹，未变则跳过（`skip_rate` 长期 > 60%）
- **失败可见** —— 三段校验（prompt 约束 / 强类型 / 业务断言），失败进 `review_queue`
- **抓取兜底** —— Crawl4AI 异常或空页面时，Playwright 直取 HTML 复用同一套抽取策略（最多 1 次）
- **断点恢复** —— LangGraph checkpoint，中断后从下一个目标续跑
- **配置化** —— 新增竞品只改 `targets.yaml`，不写代码
- **两种入口** —— CLI / HTTP + Celery 定时 / stdio MCP，共用一个内核
- **飞书推送** —— 有字段级变化才推（默认），也可切成每天必推；卡片含变更明细 + 报告直达链接

## 工作原理

```
targets.yaml / 单页 URL
    │
    ▼
┌────────┐   ┌──────────────────────────────────────────┐   ┌──────┐   ┌─────────┐   ┌────────┐
│  plan  │──▶│  fetch（每个目标循环一次，逐目标 checkpoint） │──▶│ diff │──▶│ analyze │──▶│ report │
└────────┘   └──────────────────────────────────────────┘   └──────┘   └─────────┘   └────────┘
                        │
                        ├─ robots.txt 检查 → 限速 ≥2s → 出站 SSRF 校验
                        ├─ Crawl4AI 抓取 HTML
                        ├─ 指纹比对 ── 未变 ──▶ skipped（不调 LLM）
                        ├─ 变更 ──▶ LLM 结构化抽取（schema 约束）
                        └─ 三段校验 ── 失败 ──▶ extraction_error 落库
```

代码分层是 **一个内核 + 三个适配器**：

```
core/           采集、抽取、diff、报告、存储（不依赖任何适配器）
adapters/mcp/   stdio MCP：只读查询 + 进程内调用内核抓取
adapters/http/  FastAPI：手动触发 / 查询 / 报告
adapters/schedule/  Celery：定时采集 + 飞书通知
cli.py          统一入口 `ci`
```

MCP 不再走 HTTP：查询只读打开 SQLite，抓取与批量采集直接在进程内调内核，少一跳、少一个必须常驻的服务。

## 安装

需要 Python ≥ 3.11。按用途选一档即可：

| 档位 | 安装 | 得到 |
| --- | --- | --- |
| 只要雷达 | `pip install -e .` | `ci` 命令：抓取 / diff / 报告 / 复核 |
| 雷达 + MCP | `pip install -e ".[mcp]"` | 额外 `competitor-intel-mcp`，接入 AI 客户端 |
| 全量（含 HTTP + 定时） | `pip install -e ".[all]"` | FastAPI + Celery + Redis 调度 |
| 开发 | `pip install -e ".[all,dev]"` | 测试 / ruff / mypy |

```bash
git clone https://github.com/ZhiJiu-An/competitor-intel.git
cd competitor-intel
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
playwright install chromium     # 或设 CRAWLER_BROWSER_CHANNEL=chrome 复用系统 Chrome

cp .env.example .env            # 填入 LLM_API_KEY
ci init-db
ci run --config config/targets.yaml
```

第二次运行若页面没变，你会看到 `skipped` 而非重新抽取——这就是增量生效。

## 配置

`config/targets.yaml`：

```yaml
defaults:
  request_interval_s: 2      # 合规：请求间隔 ≥ 2s
  respect_robots: true       # 遵守 robots.txt
  timeout_s: 30
  cache_mode: bypass         # Crawl4AI L1 缓存无 TTL，默认绕开；增量交给业务指纹

targets:
  - competitor: deepseek
    pages:
      - type: pricing        # pricing / changelog
        url: https://api-docs.deepseek.com/quick_start/pricing
        main_selector: "main"    # 指纹容器：只比较正文区域，忽略导航/页脚
```

新增竞品只需追加一段，无需改代码。

### 项目根目录（home）解析

`data/`、`config/`、`.env` 都相对于 **home** 解析，优先级：

1. 环境变量 `COMPETITOR_INTEL_HOME`
2. 从当前目录向上查找含 `config/targets.yaml` 的目录
3. 源码仓库根
4. `~/.competitor-intel`

因此从任意目录运行 MCP / CLI 都能找到同一份数据。

## 使用

### CLI（雷达）

```bash
ci init-db                                   # 初始化 SQLite
ci run                                       # 全量运行（走指纹短路）
ci run --competitor deepseek --force         # 指定竞品并强制忽略指纹
ci review                                    # 查看复核队列
ci review --retry-failed                     # 强制重跑失败页面
ci report --since 2026-10-01 --out data/reports/latest.md   # 从历史快照重新出报告
ci serve --port 8000                         # 启动 HTTP 服务
ci serve --schedule                          # 同时启动 Celery worker + beat（需 Redis）
```

### 输出示例

报告（`data/reports/2026-11-02.md`，示例数据）：

```markdown
# 竞品情报报告（2026-11-02）

## 概要

- 监控页面快照：3 个
- 发生变更：1 个页面

## 变更明细

### acme-cloud — https://acme-cloud.example/pricing

基线 2026-10-26 → 当前 2026-11-02

| 字段 | 旧值 | 新值 |
| --- | --- | --- |
| pricing_tiers[Pro].amount | 25.0 | 29.0 |
| pricing_tiers[Pro].key_limits | 3 席位; 50GB 存储 | 5 席位; 100GB 存储 |

**变更解读**：Pro 档涨价 16%，同时席位与存储配额各提升约一倍，属于"提价 + 加量"的同步调整……
```

按量计费页面（如 token 计价）会把多个维度压进同一行，保证「一个模型一行」且仍是数值型（示例数据）：

```
| 竞品 | 套餐 | 价格 |
| --- | --- | --- |
| acme-llm | acme-flash | 输入·缓存未命中 0.34 CNY/1M tokens；输出 1.02 CNY/1M tokens |
```

## 接入 MCP 客户端（眼睛）

```bash
pip install -e ".[mcp]"
```

注册了两种等价入口：`competitor-intel-mcp`（独立命令）与 `ci mcp`（同一个 stdio 服务）。

在 Trae / Claude Desktop / Cursor 的 MCP 配置里加一条（路径换成实际绝对路径）：

```json
{
  "mcpServers": {
    "competitor-intel": {
      "command": "/path/to/competitor-intel/.venv/bin/competitor-intel-mcp",
      "args": [],
      "env": {
        "COMPETITOR_INTEL_HOME": "/path/to/competitor-intel"
      }
    }
  }
}
```

> 日志只写 stderr，stdout 全部留给 MCP 协议帧；客户端若显示无响应，先检查有没有别的东西往 stdout 打印。

### 工具

| 类型 | 工具 | 用途 |
| --- | --- | --- |
| 只读 | `list_targets` | 竞品名与页面 URL 的唯一权威来源 |
| 只读 | `list_snapshots` | 快照元数据：日期、状态、内容指纹 |
| 只读 | `list_changes` | 跨竞品 / 跨页面汇总变化 |
| 只读 | `get_snapshot` | 某竞品最新结构化内容（套餐、价格、功能项） |
| 只读 | `diff_snapshots` | 单页面两时点的字段级对比 |
| 只读 | `list_review_queue` | 抽取失败待复核记录 |
| 只读 | `get_report` | 最新一份 Markdown 报告 |
| 只读 | `get_run` | 采集任务状态与计数指标 |
| 写 | `crawl_page` | **眼睛**：临时抓任意页面，当场返回快照 + 变化 + 报告链接 |
| 写 | `trigger_run` | **雷达**：批量采集已配置竞品，默认等跑完返回结果 |

另有 resource `ci://reports/latest`，客户端可直接挂载最新报告。

两个写工具标注 `read_only_hint=false / open_world_hint=true`，客户端通常会先确认。

- `crawl_page` 在进程内同步执行，页面不必写进 `targets.yaml`；结果照常落库，下次能算出差异。页面指纹未变时不会重复抽取，而是直接返回最近一次快照（`note` 里会说明）。
- `trigger_run` 同日同竞品幂等，重复触发返回同一个 `run_id`；等待超时会返回 `TIMEOUT` 并带 `run_id`，用 `get_run` 继续查，**不要重复触发**。
- `trigger_run(force=true)` 会打破幂等：即使当天已成功也重新排队、同一个 `run_id` 重跑（改了配置或换了 key 后使用）；正在运行的 run 不会被抢占。
- `list_changes` 的 `since` 是窗口起点（含）：基线取 `since` **之前**最近一条快照，所以 `since=今天` 也能看到今天发生的变化。

### 返回与错误

成功返回普通 JSON。失败走工具错误通道，body 是可解析的 JSON：

```json
{ "code": "NOT_FOUND", "message": "没有早于 2026-11-03 的快照可作基线",
  "hint": "该页面只有一条快照，等下一次采集后再对比", "retryable": false }
```

| code | 含义 |
| --- | --- |
| `DB_NOT_FOUND` / `TARGETS_NOT_FOUND` | 快照库 / 站点配置不存在（先跑 `ci init-db` / 检查 home） |
| `NOT_FOUND` | 查询无结果，通常附带可用日期或候选项 |
| `BAD_ARGUMENT` | 参数有歧义，如多页面竞品未指定 `page_url` |
| `BLOCKED` | 出站地址被 SSRF 校验拦截（回环 / 内网 / 非 http(s)） |
| `TIMEOUT` | 抓取或批量采集超时（批量采集用 `run_id` 继续查） |
| `EXTRACTION_ERROR` | 页面能抓但抽取失败，已进复核队列 |
| `REPORT_NOT_FOUND` | 还没有生成过报告 |

## HTTP API + 定时调度

```bash
pip install -e ".[http,schedule]"

# 一键起 redis + api + worker + beat（日志在 scripts/.logs/）
./scripts/start.sh up
./scripts/start.sh status
./scripts/start.sh down
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/runs` | 触发采集。同日同竞品幂等：首次 `202`，重复 `200` 且 `run_id` 不变；带 `force=true` 则同 `run_id` 重跑 |
| GET | `/runs/{run_id}` | 状态（`queued/running/succeeded/partial/failed`）+ 计数指标 |
| POST | `/crawl` | 临时抓单页，同步返回快照与报告链接 |
| GET | `/snapshots` | 快照元数据，支持 `competitor/since/until/limit` |
| GET | `/reports/latest` | 最新 Markdown 报告（`text/markdown`） |
| GET | `/reports/{name}` | 报告 HTML 直链（飞书卡片按钮用，不鉴权） |
| GET | `/healthz` | 探活（含数据库可用性） |

```bash
curl -X POST localhost:8000/runs -H 'Content-Type: application/json' \
     -d '{"competitor":"deepseek"}'
# {"run_id":"...","status":"queued","idempotent":false}

curl localhost:8000/runs/<run_id>
# {"status":"succeeded","processed":1,"extracted":1,"skipped":0,
#  "metrics":{"skip_rate":0.0,"fallback_rate":0.0,"extraction_error_rate":0.0}, ...}
```

`beat` 默认每天 09:00（`Asia/Shanghai`）触发 `competitor_intel.daily_run`，只创建当日 run 再投递，重复触发不会重复抓取。

> **安全提示**：`api` 默认只绑定 `127.0.0.1`。若改为对外暴露（`0.0.0.0`），**必须**设置 `API_TOKEN`，否则任何人都能触发采集并读取快照与报告。

**状态语义**：`succeeded`（含"全部 skipped"，页面无变化属正常）/ `partial`（有兜底、抽取失败或 robots 拦截）/ `failed`（有目标但一条都没抽到，或空批次）。当日 `failed` 允许重新 POST 重试，`run_id` 复用。

## 飞书推送

配好 webhook 后，定时 run 结束会按 `FEISHU_NOTIFY_MODE` 推一张卡片（状态色 / 计数 / 三个比率 / 变更明细表 / 报告按钮）：

```bash
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
FEISHU_KEYWORD=竞品分析        # webhook 若开了关键词校验，卡片标题会自动带上
FEISHU_NOTIFY_MODE=changes     # changes=无变化且正常才静音（默认）；always=每天都推
REPORT_BASE_URL=http://127.0.0.1:8000   # 报告按钮的地址前缀；远程访问时改成实际域名
```

`changes` 模式下只有「无字段级变化 **且** run 正常结束」时才静音（只记一条 `feishu.skipped` 日志）；`failed` / `partial`（含抽取失败、被 robots 拦截）照常推送，避免故障被静音。`always` 模式适合想每天确认采集健康度的场景。

推送失败只记 warning，**不影响 run 状态与返回值**。未配置 webhook 时内核完全不引入通知适配器。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `COMPETITOR_INTEL_HOME` | 自动解析 | 项目根目录，所有相对路径的基准 |
| `LLM_API_KEY` | — | 必填（采集路径），OpenAI 兼容接口的 key |
| `LLM_BASE_URL` | — | 兼容接口的 base URL，如 `https://api.deepseek.com` |
| `LLM_MODEL` | `deepseek-chat` | 模型 ID；带 `/` 则原样作为 litellm provider |
| `DB_PATH` | `data/snapshots.db` | SQLite 路径 |
| `TARGETS_PATH` | `config/targets.yaml` | 竞品与页面配置 |
| `REPORT_DIR` | `data/reports` | 报告输出目录 |
| `API_TOKEN` | 空 | 设置后 API 需带 `X-API-Token` 请求头 |
| `REPORT_BASE_URL` | `http://127.0.0.1:8000` | 飞书卡片里报告按钮的地址前缀 |
| `FEISHU_WEBHOOK` | 空 | 留空则不推送 |
| `FEISHU_KEYWORD` | `竞品分析` | webhook 关键词校验时自动加进标题 |
| `FEISHU_NOTIFY_MODE` | `changes` | `changes`=无变化且正常才静音；`always`=每次都推 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Celery broker / backend |
| `DAILY_CRON_HOUR` | `9` | beat 每日触发小时 |
| `CRAWLER_BROWSER_CHANNEL` | `chromium` | 浏览器通道；本机没装 Playwright 自带 chromium 时设 `chrome` |
| `CI_API_TIMEOUT_S` | `20` | MCP 只读查询超时 |
| `CI_CRAWL_TIMEOUT_S` | `240` | `crawl_page` 等待上限（浏览器 + LLM） |
| `CI_RUN_TIMEOUT_S` | `240` | `trigger_run` 等待上限 |
| `ALLOW_PRIVATE_NETWORKS` | 空 | 设为 `1` 允许抓回环 / 内网地址（默认拒绝，防 SSRF） |

## 数据契约

```python
CompetitorSnapshot
├── competitor_name / page_url / snapshot_date / page_type
├── pricing_tiers: PricingTier[]
│   ├── name / amount / currency / billing_period / key_limits / pricing_model
│   └── components: PriceComponent[]        # 按量计费的多维价格（订阅制留空）
│       └── unit / amount / currency / condition
├── feature_items: FeatureItem[]
│   └── name / description / category
└── target_user_note
```

两个刻意的取舍：

- `amount` 用 **float** 换可比较性，且用 `StrictFloat` 拒绝 `"99"` 这类字符串，防止 LLM 输出被静默强转；
- "免费 / 面议" 显式 `null`，**不臆造 0**。

快照表新增 `origin` 列区分来源：`scheduled`（定时 / 批量）与 `adhoc`（临时抓单页）。日报只统计 `scheduled`，临时抓取不会污染当日报告；同一页面同一天重复抓取时，`scheduled` 覆盖 `adhoc`，反向不覆盖。

## 项目结构

```
competitor-intel/
├── config/targets.yaml          # 站点配置
├── src/competitor_intel/
│   ├── core/
│   │   ├── models.py            # Pydantic 契约（最先写、反复打磨）
│   │   ├── config.py            # targets.yaml + 环境变量 + home 解析
│   │   ├── fingerprint.py       # 容器文本归一化 + sha256
│   │   ├── collector.py         # Crawl4AI 主路径 + Playwright 兜底
│   │   ├── store.py             # SQLite 读写 / review_queue / runs
│   │   ├── query.py             # 只读查询（mode=ro）
│   │   ├── diff.py              # 字段级对比（纯函数）
│   │   ├── analyst.py           # 基于 diff 的变更解读
│   │   ├── reporter.py          # Markdown 报告
│   │   ├── graph.py             # LangGraph 五节点编排
│   │   ├── orchestrator.py      # 幂等执行 + 三态判定 + 指标
│   │   ├── netguard.py          # 出站 SSRF 校验
│   │   └── errors.py            # 统一错误码
│   ├── adapters/
│   │   ├── mcp/                 # server / queries / bridge（进程内直调）
│   │   ├── http/                # FastAPI
│   │   └── schedule/            # Celery tasks + 飞书 notifier
│   └── cli.py                   # `ci` 命令入口
├── scripts/start.sh             # 一键启停本地四进程
├── tests/                       # 单测 + 集成测试（假 LLM / mock 页面）
└── docker-compose.yml           # redis / api / worker / beat
```

## 测试

```bash
pytest              # 单测 + 集成测试（不触网、不花 token）
python scripts/smoke_e2e.py   # 本地 HTTP 假站点 + 假 LLM 的端到端冒烟
CI_SMOKE=1 pytest   # 额外跑真实站点冒烟（会消耗 token）
```

## 合规

- 仅采集**公开可访问**页面，不触碰登录态与付费墙
- 尊重 `robots.txt`，禁止则跳过并记录 `blocked`
- 默认请求间隔 ≥ 2s、单目标超时 30s
- 默认拒绝回环 / 内网地址，避免被当成 SSRF 跳板
- 请自行确认目标站点的服务条款；本项目不提供绕过反爬的能力

## 已知限制

- 页面改版导致 `main_selector` 失效时，指纹可能取错容器（会告警并降级整页指纹）
- Crawl4AI 的 L1 缓存无 TTL，因此默认 `cache_mode: bypass`，增量只由业务指纹负责
- SSRF 校验在「发起抓取前」解析一次域名，浏览器自己还会再解析一次；理论上存在 DNS rebinding 时间窗（自建内网服务场景请配合 `ALLOW_PRIVATE_NETWORKS` 显式开关使用）
- `targets.yaml` 的 URL 在加载期只校验 http/https 协议；需要抓内网地址时请显式设置 `ALLOW_PRIVATE_NETWORKS=1`
- macOS 本地跑 Celery worker 需 `--pool=solo`（fork 子进程里启动 Playwright 易崩），`scripts/start.sh` 已处理
- MCP 为单机 stdio，不做多用户鉴权与并发调度

## 文档

- [CONTRIBUTING.md](./CONTRIBUTING.md)：架构边界与开发约定
- [LICENSE](./LICENSE)：Apache-2.0
