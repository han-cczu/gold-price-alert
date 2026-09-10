# 重构交付与验证记录

日期：2026-09-09。状态：阶段 0–5 已交付并完成本地验收；阶段 6 的 Docker 验收受环境阻碍。

2026-09-10 更新：[PR #2](https://github.com/han-cczu/gold-price-alert/pull/2) 已合并到 `05802d0`，其 [主分支 CI](https://github.com/han-cczu/gold-price-alert/actions/runs/34442124715) 四项全部通过，包括隔离 Mock 容器的构建和运行。本文件下方保留 2026-09-09 重构时的历史验收记录；后续可靠性修复及最新本地检查见 [可靠性修复记录](reliability-fixes.md)。

本记录对应分支 `codex/modular-refactor` 的代码提交 `4a103ad`。重构前业务快照为 `5e2fa7f`；实施基线 `03c606f` 在此基础上增加计划文档。全套测试为 193 项通过，提交记录见本文件所属 Git 历史。本轮按模块并行开发后集成：`4a103ad` 提交代码、测试和构建配置，后续文档提交记录契约与验证结果；没有形成原计划所述的逐阶段小提交序列。

## 1. 实现范围

| 阶段 | 当前交付 | 当前状态 |
|------|----------|----------|
| 0：行为与测试基线 | [API 契约](api-contract.md)、隔离临时配置/数据库与替身、明确成功/失败断言 | 基线已验证 |
| 1：装配与生命周期 | `web.create_app`、`ApplicationRuntime`、实例级依赖、启动失败清理、后台任务回收 | 本地验收通过 |
| 2：采集与存储 | 策略/来源工厂/调度分离、报价保存与去重、数据库线程/会话边界、全窗口图表、备份恢复 | 本地验收通过 |
| 3：告警与通知 | 规则/渠道分离、告警与冷却事务、有效配置统一、独立渠道重试与发送日志 | 本地验收通过 |
| 4：分析与配置 | 供应商/提示词/解析分离、缓存关联配置、相同请求合并、配置原子保存、USD 本地分析 | 本地验收通过 |
| 5：API 与前端 | 业务下沉到服务、ES modules/CSS、管理员凭证入口、单位换算、时间与图表状态 | 本地验收通过 |
| 6：交付验证 | 全套检查、安装包、浏览器流程、容器和文档核对 | 其余本地检查通过；Docker 受环境阻碍 |

项目仍采用 FastAPI、SQLAlchemy、SQLite、原生 JavaScript 和单服务部署；没有引入微服务、前端框架或新数据库产品。

## 2. 当前架构与兼容入口

| 位置 | 职责 |
|------|------|
| [web.py](../src/gold_monitor/web.py)、[runtime.py](../src/gold_monitor/runtime.py)、[dependencies.py](../src/gold_monitor/dependencies.py) | 应用创建、lifespan 装配、资源归属、路由获取当前实例 |
| [collector.py](../src/gold_monitor/collector.py)、`collection/`、`data_sources/` | 获取报价、策略选择、定时运行、去重和资源关闭 |
| [storage/database.py](../src/gold_monitor/storage/database.py) | 数据库会话、事务、查询、图表聚合与采样 |
| [alert.py](../src/gold_monitor/alert.py)、`alerts/`、`notifications/` | 告警评估、冷却状态、渠道适配与发送结果 |
| [services/notifications.py](../src/gold_monitor/services/notifications.py) | 通知有效配置、保存后生效、测试与实际发送入口 |
| `analysis/`、[services/analysis.py](../src/gold_monitor/services/analysis.py)、[llm_config.py](../src/gold_monitor/llm_config.py) | 供应商调用、报告解析、缓存、请求合并及配置文件 |
| `services/prices.py`、`services/market.py`、`services/model_probes.py` | 行情、换算、模型探测等 API 业务 |
| `static/js/`、`static/css/`、[time_utils.py](../src/gold_monitor/time_utils.py) | 前端模块、公共时间约定 |

兼容与调整：

- 保留 `gold-monitor` 命令、`gold_monitor.web:app`、既有 HTTP 路径及 `/ws` 消息类型；拆出的资源通过 `/static` 提供。
- 新入口为 `create_app(config, ...)`。配置复制后归当前应用所有，数据库在 lifespan 启动时初始化。数据库、服务、任务、鉴权、限流和 WebSocket 连接从当前 runtime 获取。
- `from gold_monitor.models import Database` 仍可使用，实际实现移至 `storage/database.py`。
- `web.db` 仅在默认应用 lifespan 内提供弃用兼容；应用未启动时不创建数据库。新代码应通过 `app.state.runtime.db` 获取对象。
- `state.py` 不再提供旧的全局数据库、缓存或服务对象。旧采集器/生命周期全局辅助入口保留给过渡调用，当前 runtime 不依赖这些入口。
- 数据库表结构未在本轮新增或迁移；新增保存语义使用既有币种和时间列。没有把对临时数据库的验证描述为对用户现有生产数据库的迁移验证。
- 跨版本检查使用 `03c606f` 代码生成临时 SQLite、明文与加密模型配置，再用当前应用启动读取。USD/CNY 报价及微秒时间、告警与冷却/窗口、通知覆盖和日志、分析历史、模型选择与测试密钥均保留，旧 Fernet 密文可解密；两个模型文件字节未变，关闭后数据库释放。两个进程分别断言导入来自对应版本，外部网络尝试为 0。

## 3. 配置解析与生效规则

### 模型配置

普通 provider 工厂的优先级为：**显式 `provider` 参数 → 已保存模型配置文件 → 应用 `Settings` 默认配置**。

- 显式传 `mock/openai/anthropic` 时选择对应实现；显式选择真实 provider 但缺少所需密钥时会报错，不隐式覆盖成另一个真实 provider。
- 未显式指定 provider 时，已有配置文件决定当前平台和模型。`Settings` 中的环境值用于不存在配置文件时生成默认配置；无有效密钥或选中 Mock 时使用 Mock。
- 更改 `GOLD_LLM_PROVIDER` 不会自动覆盖已保存的平台选择。希望使用环境配置重新初始化时，应显式重置模型配置，或使用新的 `GOLD_LLM_CONFIG_PATH`。
- 应用服务按有效配置指纹及模型区分缓存；相同配置下的并发请求合并执行。旧任务完成后不会把其结果发布为新配置的缓存。
- 文件保存先写临时文件，再原子替换，成功后才发布内存配置。保存失败、现有文件损坏或无法解密有明确错误响应；不会把损坏文件当成“尚未配置”。
- 脱敏密钥不覆盖已保存真实值；未保存的表单配置仍可用于测试连接和获取模型列表。
- 搜索未使用或失败降级时，保留 `web_search_used=false` 和提示；不会把普通模型回答标记为已获取实时市场数据。

配置加密受 `GOLD_ENCRYPT_API_KEYS`、`GOLD_SECRET_KEY` 及既有文件格式影响。本文只描述当前实现和测试覆盖，不宣称密钥在所有配置下均以密文存储。

### 通知与采集配置

- 通知首先读取当前应用 Settings；某渠道存在数据库配置时，以数据库 `enabled` 为准，字段覆盖 Settings 中对应值，未保存字段保留环境默认值。
- 保存、测试发送和实际告警使用同一配置解析及渠道工厂。配置更新影响后续新发送，已经开始的发送使用当时的渠道快照。
- 禁用渠道不参与实际发送；默认控制台渠道仍可用。设置主密钥时敏感通知字段通过加密保存；界面返回脱敏值。
- 采集配置先完整校验，再修改间隔/策略；策略切换等待在途采集结束后替换并关闭旧数据源。初始数据源由实例 Settings 决定。

## 4. 有意改变的行为

| 项目 | 当前行为与影响 | 相关证据 |
|------|----------------|----------|
| 配置部分生效 | 非法策略或源构造失败不会先改变采集间隔 | `tests/test_collector.py` |
| 报价保存 | 时间、币种、来源保留；有时区输入转成存储使用的无时区 UTC | `tests/test_collector.py`、`tests/test_storage.py` |
| 去重与计数 | 与最后入库价格比较；`saved_count/skipped_count` 区分保存与跳过，补间隙按实际入库计数 | `tests/test_collector.py` |
| 补间隙 | 仅采集当前样本；无法从只提供现价的数据源恢复历史价格 | `collector.fill_gaps` |
| 混合币种 | 图表和本地波动分析按 USD 筛选；分析窗口与最近报价在 SQL 过滤后再应用条数限制；USD 告警规则跳过其他币种 | `tests/test_storage.py`、`tests/test_analysis_api.py`、`alert.check_price` |
| 告警一致性 | 告警记录和冷却状态在同一事务提交，成功后更新内存冷却；失败时一起回滚；取消时先完成事务、内存发布与通知任务登记，周期保存不回写旧冷却状态 | `tests/test_storage.py`、`tests/test_notification_service.py` |
| 历史币种与报价标识 | 历史表按原币种显示；银行区域明确是预设价差推算的参考报价 | `tests/frontend/sections.test.mjs`、`static/index.html` |
| 单位换算 | 使用每盎司 `31.1035 g = 0.0311035 kg` 的一致系数，修复公斤换算 | `static/js/state.js`、`services/market.py` |
| 公开时间 | API/WS/导出使用带 UTC `Z` 的时间；旧无时区时间按 UTC 解读，显示层按浏览器时区格式化 | `time_utils.py`、`tests/frontend/state.test.mjs` |
| 模型配置失败 | 读写失败返回 503；当前报价源不可用返回 503；非法业务参数返回 400，参数 schema 校验仍可能返回 422 | `web.py`、`routers/data.py` |
| 管理员请求 | 页面 API client 统一携带 `X-Admin-Key`，处理鉴权、限流、超时与取消；凭证仅在当前页面内存中保存 | `static/js/api.js`、`tests/frontend/api.test.mjs` |
| WebSocket | 每个连接独立队列和发送任务；发送超时或队列满会关闭慢连接，避免等待它完成整次广播 | `realtime.py`、`tests/test_runtime.py` |

### 图表完整窗口与样本数

`Database.get_chart_data(start, end, max_points=2000, currency="USD")` 在数据库中按时间/ID 排序并分桶，每桶保留最低和最高报价，同时保留整个窗口首尾。返回点数不超过 `max_points`，不再只查询最近 5000 条后冒充全年或五年数据。

- `high/low/average/current_price/price_change/price_change_percent` 基于完整查询窗口的原始报价计算。
- `count` 是窗口内匹配币种的原始入库记录数；`len(prices)` 是绘图采样点数，两者可能不同。
- `window_start/window_end` 是请求窗口边界，`timestamps` 首尾是实际第一条/最后一条报价时间。
- 少于 4 个绘图点时优先保留首尾，无法保证同时绘出所有峰谷，但数值统计仍使用完整窗口。
- 前端使用服务端聚合统计。WS 新增 `recorded`：已入库报价参与追加统计，未入库报价只更新当前价和涨跌，不增加样本数或折线记录。窗口滚动和无法判断是否已计入的迟到记录触发重新查询；旧消息没有该字段时按已入库处理。相关并发合并由 Node 行为用例覆盖，浏览器已验证实际绘图和实时更新。

### 备份、导出和恢复

- 清理及导出使用注入的数据库；导出支持仅提供开始或结束日期，条数限制直接进入 SQL。
- SQLite 备份基于实际 Engine 连接调用 SQLite backup API，覆盖 WAL 中已提交数据，也支持内存数据库；备份完成后才发布目标文件。备份名不能越出指定目录或覆盖当前数据库。
- 非 SQLite 保留 JSON 价格导出的兼容路径，并返回 `backup_scope="prices_only"`、`record_limit=100000`。这不包含告警、通知配置等全部表，不是完整数据库备份。
- 恢复方法没有 HTTP 路由。必须停止所有数据库使用者和其他进程，先 `await db.aclose()`，校验 SQLite 文件格式/完整性及报价表，再恢复到 Engine 指定文件。恢复前保留 `.bak`，恢复后创建新 Database；不能在当前已关闭实例上继续查询。
- 小时/日聚合尚未实现，`archived_count` 仍为 0；未把已有参数名称当成已完成的归档能力。

## 5. 验证记录

下表区分已实际取得的结果和待完成项。定向组合有重叠，不应相加成“全项目通过数量”。

| 日期/检查点 | 环境与范围 | 已取得结果 |
|-------------|------------|------------|
| 2026-09-09 基线 `03c606f` | Python 3.13，隔离副本；业务代码等同 `5e2fa7f` | 85 项测试通过；Ruff 检查通过，45 文件格式检查通过；MyPy 34 源文件通过 |
| 2026-09-09 采集/存储/生命周期检查点 | `tests/test_collector.py tests/test_data_sources.py tests/test_storage.py tests/test_data_lifecycle.py`；临时 SQLite/Mock，使用独立 `--basetemp` | 58 项通过；对应 Ruff 通过、16 文件格式通过、MyPy 8 源文件通过 |
| 2026-09-09 后续币种查询扩展 | `tests/test_storage.py`；增加 SQL 币种筛选早于 limit/offset 的回归 | 22 项通过；对应 Ruff/MyPy 通过 |
| 2026-09-09 分析检查点 | Python 3.13.5；`test_analyzer.py test_analysis_service.py test_llm_config.py test_analysis_api.py`，独立临时目录 | 60 项通过、2 条 warning；相关 5 文件 Ruff/格式通过，MyPy 3 源文件通过；不代表真实供应商联网验证 |
| 最终 Python 全套 | Python 3.13.5；隔离临时数据库/配置，外部调用使用替身 | **193 passed in 17.02s** |
| 最终 Ruff/格式/MyPy | 与 CI 一致的 src lint、全仓格式、src 类型检查 | Ruff 通过；80 个 Python 文件格式通过；MyPy 63 源文件无错误（未注解函数提示保留） |
| 前端行为与浏览器流程 | Node 24.15.0 与 Codex In-app Browser；Mock 服务，临时数据库 | 3 项 pytest 包含 32 个 Node 行为用例；图表、单位、401/重新认证、凭证刷新清除、Mock 探测和分析刷新、WS 更新通过；页面 error/warn 日志为空 |
| wheel 与源码目录外启动 | 构建 wheel、pip --target 安装；工作目录独立于 src，断言导入来自安装目录 | CLI entry point --version 正常；实际 Uvicorn 启动；10 个 HTTP 端点、9 JS + 1 CSS、鉴权 401/200、WS recorded/pong 及正常关闭通过；复用本机已安装依赖，未宣称全新操作系统验证 |
| OpenAPI 兼容性 | 分别从基线与当前 src 导入生成定义 | 两版 46 个 HTTP 操作全部保留；请求字段/required、参数默认保持；首页 HTML 声明已恢复，新增字段与约束变化见本文件 |
| Docker 构建与容器运行 | 本机 Docker Desktop | 未通过验收：引擎未启动，详见下文 |

分析检查点的实际命令：

```powershell
$analysisTmp = Join-Path (Get-Location) ('.tmp_pytest\analysis-priority-' + [guid]::NewGuid().ToString('N'))
python -m pytest tests/test_analyzer.py tests/test_analysis_service.py tests/test_llm_config.py tests/test_analysis_api.py -q --disable-warnings --basetemp $analysisTmp
python -m mypy src/gold_monitor/analysis/factory.py src/gold_monitor/llm_config.py src/gold_monitor/services/analysis.py --ignore-missing-imports
```

系统默认 Pytest 临时目录曾遇到 Windows 权限拒绝；定向测试改用项目内新生成的 `--basetemp` 路径后通过，没有据临时目录错误修改业务断言。

最终检查使用以下命令；`<独立临时目录>` 实际使用项目内 `.tmp_pytest/release-verification` 等全新路径：

```bash
python -m pytest tests/ -q -p no:cacheprovider --basetemp <独立临时目录>
python -m ruff check src/
python -m ruff format . --check
python -m mypy src/ --ignore-missing-imports
node --test tests/frontend/*.test.mjs
```

### Docker 阻碍

本机 Docker Desktop 引擎未能启动，客户端找不到 `dockerDesktopLinuxEngine` named pipe。backend 日志另有 Inference manager socket 路径访问失败。当前证据只支持“本机引擎不可用、容器验收未完成”；没有证明项目镜像可以成功构建或容器可正常运行，也未将日志中的错误推断为唯一根因。

### 2026-09-10 合并前 CI 环境修正

- 首轮 CI 自动安装 Ruff 0.16.6，本地基线使用 0.15.5。Ruff 的次版本可改变默认规则和格式，详见[官方版本约定](https://docs.astral.sh/ruff/versioning/)。项目将开发依赖和 `required-version` 同时固定为已验收的 0.15.5，避免本地与 CI 使用不同检查规则；升级须单独验收。
- MyPy 2.3.1 对条件表达式中的通知列表推断更窄，已显式标注为 `list[NotificationChannel]`，保留不同渠道实现的原有运行行为。
- 安全扫描曾安装但未使用 `safety`，它引入了有漏洞的 `nltk`；该依赖不属于项目运行依赖。删除这个无用工具，并将 runner 和镜像虚拟环境的 `setuptools` 升级至扫描报告要求的 83.0.0 或以上。
- 保留 `pip check` 和无忽略项的 `pip-audit`，扫描当前安装环境中的全部项目依赖；发现漏洞仍阻断检查。Docker job 额外启动构建后的 Mock 容器，验证健康状态、报价和打包后的页面资源。

## 6. 部署范围与待完成项

- 当前应用按**单进程、单 worker、一个实例写数据库和模型配置文件**部署。多个 worker 会各自启动采集、通知和定时任务；进程内锁、缓存、限流和 WebSocket 连接不提供分布式协调。
- 通知任务保存在进程内，发送结果有数据库日志，但没有持久待发送队列。进程崩溃后不保证补发或恰好发送一次。
- 关闭顺序会先停止生成工作、再回收已有任务和客户端；个别发送/连接设置了超时。在途数据库事务或 SMTP 线程需要排空，不承诺所有退出路径都有统一的固定时间上限。
- 图表返回点数有界，但数据库仍需处理完整窗口以计算统计与采样；没有做长期生产数据的性能基准。
- 真实行情、模型、邮件和 Webhook 的运行可用性未用本轮隔离测试验证。相关测试使用替身，不产生真实通知或付费调用。
- 2026-09-09 本地 Docker 未验收；2026-09-10 基线 `05802d0` 的 GitHub CI 已完成隔离 Mock 镜像构建和容器运行。真实供应商连通性、生产长期负载或跨进程协调仍未验收。
