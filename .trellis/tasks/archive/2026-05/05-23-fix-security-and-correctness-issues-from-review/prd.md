# 修复审阅发现的安全与正确性问题

## Goal

针对代码审阅发现的问题进行系统性修复，覆盖：正确性 bug、安全加固、工程/仓库卫生，以及把 4052 行的 `web.py` 单体重构为可维护的结构。目标是让系统在默认部署下更安全、行为不再误导用户、关键容错逻辑真正生效，并提升可维护性与测试覆盖。

## What I already know（来自审阅 + 代码核验）

- 测试基线：32 passed，ruff 通过。
- `web.py` 4052 行，内嵌 HTML/CSS/JS ~2236 行（55%）。
- provider api_key 返回前端已脱敏（`to_safe_dict`）——非明文泄露（已核验，排除误报）。

## Requirements（按决策确定）

### A. 正确性 bug
- A1. `collector.py` `_fetch_parallel_first`：pending 任务在回退分支前被 cancel，导致回退死代码。最快源失败即整体失败。需调整为先回退、后取消（PARALLEL_FIRST 是默认策略）。
- A2. 通知阻塞采集循环：`check_price` 同步 `await send_with_retry`（含退避 sleep），慢渠道卡住采集。改为不阻塞（后台任务）。
- A3. 通知日志 `alert_id` 恒为 NULL：恢复 `_record_alert` 返回的 id 并传入 `send_with_retry`。
- A4. `retry_count` off-by-one（alert.py:383）。
- A5. 版本号三处不一致（pyproject 0.2.0 / web.py app 0.1.0 / cli 0.1.0）统一。
- A6. 时区：analyzer.py 用 `datetime.now()`（本地）混入 naive UTC 体系，统一。
- A7. `fill_gaps` 名不副实（只抓当前价存当前时间戳）：决策为"诚实化"——按真实行为重命名/改文档，或在无法回填时不谎称补全。

### B. 安全加固（保持 enable_auth 默认 False + 加固）
- B1. 补全 `ADMIN_PATHS` 遗漏项：`/api/data/export`、`/api/data/backups`、`/api/data/stats`、`/api/notifications/logs` 等；`/metrics` 评估是否加保护。
- B2. 关键写接口加 `Depends(require_admin)` 双重保护，不只靠中间件。
- B3. CORS：`allow_origins` 改为可配置（settings），默认收紧（非 `*` + credentials 组合）。
- B4. 加密方案：XOR → `cryptography.fernet.Fernet`，带完整性校验；处理旧密文兼容（旧数据无法解密时安全降级 + 日志）。
- B5. 自动生成临时密钥的"假象"：未配置 secret/admin key 时的行为更明确（日志告警 + 文档），避免误以为受保护。
- B6. `.env.production` 改名为 `.env.production.example` 或纳入 .gitignore（当前为占位符，无真实泄露，但规则未覆盖）。

### C. 联网分析（真正实现）
- C1. 为 `smart_analyze` 接入真实联网搜索：按 provider 能力分流（Anthropic web_search 工具 / OpenAI 联网能力 / 不支持的兼容接口降级并明确标注）。
- C2. 前端措辞与实际能力一致（支持时显示"联网"，不支持时标注"基于模型知识"）。
- C3. 错误/超时处理，避免拖垮请求。
- 详见 [`research/web-search-apis.md`](research/web-search-apis.md)。

### D. 工程 / 仓库卫生
- D1. 仓库杂物：根目录 `nul`、`test_web.db`、`gold_prices.db`、`.tmp_pytest/` 处理（确认 gitignore 覆盖 / 删除残留）。
- D2. 静默吞错（`except Exception: pass`：bank.py:105、base.py:40 等）补日志。
- D3. 补关键路径测试：安全中间件鉴权/限流、VolatilityDetector.should_alert 多条件、collector 三种策略、fallback 切换。
- D4. test_web.py module 级共享真实 db 的状态耦合问题。

### E. web.py 大重构（高风险，最后做）
- E1. 拆分：`routers/`（按领域分路由）、`static/` 或模板（抽离内嵌 HTML/JS）、保持 API 兼容。
- E2. 全局可变状态/初始化收敛到可测试结构（尽量用 Depends）。
- E3. 重构后所有现有测试仍通过 + 行为不变。

## Acceptance Criteria

- [ ] 全部既有测试通过；新增关键路径测试通过；ruff 通过。
- [ ] PARALLEL_FIRST 在最快源失败时能正确回退到其它源（有测试覆盖）。
- [ ] 通知发送不再阻塞采集循环；notification_logs.alert_id 正确关联。
- [ ] 开启 enable_auth 后，所有管理/敏感读接口受保护（含原遗漏项），有测试覆盖。
- [ ] 加密改为 Fernet，旧密文安全降级，不抛未捕获异常。
- [ ] smart analysis 在支持的 provider 上真正联网；不支持时明确标注、不谎称。
- [ ] 版本号统一；CORS 可配置且默认安全。
- [ ] web.py 拆分后对外 API/页面行为不变，测试全绿。

## Definition of Done

- 每个 PR：相关单测/集成测试更新；ruff/类型检查通过；行为变更更新文档（README / .env.example）。
- 重构与安全默认值变更评估回滚影响。
- 每次改动后按用户全局要求"彻底自查"。

## Technical Approach（分阶段 / 多 PR）

- PR1（A 正确性）：低风险 bug 修复 + 测试。
- PR2（B 安全）：鉴权加固、CORS 可配置、Fernet 迁移、临时密钥告警、.env.production。
- PR3（C 联网）：依据 research 接入真实 web search + 降级 + 测试。
- PR4（D 工程）：仓库卫生、补日志、补关键路径测试。
- PR5（E 重构）：web.py 拆分（最后、独立、行为不变）。

顺序原则：先低风险高价值，再高风险重构。每个 PR 独立可验证。

## Decision (ADR-lite)

- **Context**: 审阅发现安全默认不安全、若干容错/功能 bug、单体文件难维护。
- **Decision**: 全范围修复；联网搜索真正实现；enable_auth 保持默认 False 但加固；加密换 Fernet；web.py 重构放最后。
- **Consequences**: 工作量大、分多 PR；联网搜索依赖 provider 能力需降级；重构有回归风险，靠测试兜底。

## Out of Scope

- 数据聚合（分钟→小时→日，data_lifecycle.py 里的 TODO）本次不做。
- 引入新前端框架（Vue/React）重写 UI——本次重构仅做后端/模板分离，不换技术栈。
- 切换数据库（PostgreSQL 迁移）。

## Technical Notes

- 已依赖 `cryptography`（pyproject），Fernet 可直接用。
- naive UTC 体系：`datetime.now(timezone.utc).replace(tzinfo=None)` 全项目一致，入库一致。
- 端点清单已枚举（web.py 路由 346–4037）。
- 联网搜索调研见 `research/web-search-apis.md`（子代理产出）。
