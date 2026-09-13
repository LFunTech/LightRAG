# 验证记录

## 当前状态

实施中。proposal/design/spec/tasks 已完成并通过 OpenSpec strict，但这不代表多 Pod 功能已可用。协调原语、核心写保护和流水线/API 已完成审查、修复及独立测试复跑；部署和独立业务进程验收尚未完成，不能作为已交付能力宣传。

## 环境与安全边界

- 在用户指定的当前目录功能分支 `feat/distributed-workspace-writes` 工作，未创建 worktree。
- 现有本地服务和 `.env` 不切换到未完成的分布式模式；不输出或提交 `.secrets`。
- PostgreSQL 17.9、HugeGraph 1.7.0 可供本机测试。端到端测试使用新建 `local_debug_lightrag_distributed_426c60c193` 数据库（仅该库启用 vector 0.8.2），图使用独立随机 workspace，既有业务图不改造。
- 为全量验证创建 `/tmp/lightrag-distributed-verification-venv`，按 `uv.lock` 安装 api/offline-storage/offline-llm/test extras，不改变运行服务的 `.venv`。
- macOS 测试启动 shell 会清除外层 DYLD 环境，临时 Python wrapper 在 exec 验证解释器前设置 `/opt/homebrew/lib`，用于已有 Cairo 动态库加载。没有修改系统配置。
- 尚未部署或执行真实 Kubernetes 多 Pod；独立进程测试与 Kubernetes 验证必须区分。

## 修改核心前的基线

| 验证 | 结果 |
|---|---|
| HugeGraph storage + registration 子集 | 42 passed |
| 完整既有 suite（忽略新增 distributed 测试） | 8680 passed、263 skipped、7 failed，254.17 秒 |
| 其中 SVG/Cairo 环境修正后，对应两个测试文件复跑 | 48 passed；原 5 个 SVG 失败已确认是动态库路径 |
| 原有 API colliding-prefix 鉴权断言 | 2 个既有失败：期待 401，实际拒绝为 403；发生于任何 core/API 修改之前 |

原始 `.venv` 全量收集曾因 9 类未安装的可选后端/provider 依赖产生 32 个 collection errors；完整 extras 的独立验证环境已解决该收集问题。smart_heading 的两个 spaCy 模型未安装，已有受标记测试跳过，不将跳过计为通过。

## 分阶段证据

### 协调基础（Task 1）

首版 commit `43e7f3561`。主 agent 使用新建专用 PG 数据库独立执行：

```bash
LIGHTRAG_TEST_COORDINATION_DSN=<dedicated-local-test-dsn> \
PYTHON=/tmp/lightrag-distributed-verification-venv/bin/python \
./scripts/test.sh tests/distributed --run-integration -o addopts='' -q
```

结果：30 passed，1.73 秒。包含离线验证、独立 PG 客户端、子任务同 key 竞争、真实子进程 SIGKILL、witness 失联、取消期间协调失败、迟到 ACK、恢复确认和审计。

独立审查发现安全关键列默认值 drift 未验证和非有限超时参数问题，修复 commit `43713a330` 增加实际 catalog 校验、显式协议初值和有限超时校验。新增回归先得到 27 failed / 5 passed，再转绿；主 agent 对修复版独立复跑为 62 passed。限定复核 I1/M1 均 ADDRESSED，Task 1 原语阶段通过，不代表后续核心/流水线已完成。

### 核心写保护（Task 2）

实现 commit `1059447a8`，包括 runtime profile/manifest、SDK operation、跨实例 GraphDB 锁、PG 真实 SQL pending/ACK 与禁写重试、PGVector 隔离即时提交、HugeGraph 分批 journal、verify-only 启动和显式维护、取消恢复目标与共享 chunk 缓存引用原子并集。

主 agent 在提交后独立复跑：

```bash
PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh \
  tests/distributed tests/kg/postgres_impl tests/kg/hugegraph_impl \
  tests/pipeline tests/llm tests/extraction tests/workspace tests/utils \
  tests/test_dataclass_positional_compatibility.py tests/test_docstring_budget.py \
  tests/test_aclear_cache_contract.py tests/test_storage_migrations_strict_read.py -q
```

结果：**3058 passed、64 skipped，53.72 秒**。另外使用专用 PG 测试连接执行 `tests/distributed --run-integration -q`，结果 **116 passed，4.85 秒**：Task 1 62 项、runtime 25 项、SDK 21 项、真实 PG/HugeGraph 业务 8 项。实际业务覆盖 SDK CRUD/cache/custom/purge、共享实体 RMW、SQL 已提交后 ACK 丢失、取消后精确 tracking 目标和缓存引用 stale snapshot；LLM/embedding 为测试 fixture。

这 8 项业务集成使用同进程中独立 runtime/pool，不能据此声称业务多 Pod 验证完成。Task 1 已有独立进程 coordinator 故障测试；业务独立进程与调度验证仍由 Task 3/4 完成。此阶段未重跑全量 suite。

独立审查发现三个 Important：finalize 未取得 admission 仍关闭连接、maintenance 在校验前复用 vector ALTER/DROP helper、shrinking edit 的恢复 journal 晚于即时图写。修复 commit `b840c5542` 后，限定复核 T2-I1/I2/I3 均 ADDRESSED，spec/quality 通过。

修复版由主 agent 独立复跑 `tests/distributed tests/kg/postgres_impl tests/utils tests/pipeline tests/test_docstring_budget.py`，**1926 passed、55 skipped，41.88 秒**；真实 `tests/distributed --run-integration` 为 **127 passed，6.28 秒**。新增真实案例验证 finalize 拒绝/取消后原 writer 仍可提交，以及 entity/relation/allow_merge 在实际图提交后 ACK 丢失或取消时，请求前已持久化精确 tracking 目标。只有 Task 2 的 2.1–2.4 在此阶段勾选，未提前勾选调度/API/部署。

### 流水线与 API（Task 3）

实现 commit `9606156da`：v002 持久暂停/重试 target/progress、领取后严格读回的有界调度、周期扫描、真实 worker 复用、完整 enqueue 窗口、API/共享文件/后台操作门、分布式状态、force_reset 拒绝、关闭排空和维护构造入口。v001 SQL/checksum 不变。

主 agent 在提交后独立复跑：

```bash
PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh \
  tests/distributed tests/pipeline tests/api tests/kg/postgres_impl \
  tests/test_docstring_budget.py -q \
  -k 'not test_destructive_route_requires_auth_under_a_colliding_prefix'
```

结果：**2825 passed、99 skipped、2 deselected，63.51 秒**。排除项恰为修改前已确认的两个 401/403 鉴权状态码基线；不是新增失败。专用 PG/HugeGraph 环境下执行 `tests/distributed tests/api/routes/test_distributed_controls.py --run-integration -q`：**156 passed，11.56 秒**。包括实际 HTTP handlers/鉴权、双 runtime 文档处理与共享实体、pause/retry、跨实例 scan 状态和错误尾路径。业务仍为同 OS 进程测试，不能替代 Task 4 的独立进程验收。

独立审查的四个 Important（删除接管/本地 reservation 泄漏、upload/text/texts 接管许可空窗、scan enqueue 吞文件错误、多模态 strict read 伪装为 skip）在 `ab22794a0` 修复。主 agent 独立复跑修复对应的 distributed/pipeline/API 子集为 **2265 passed、106 skipped、2 deselected，57.65 秒**；真实集成 **169 passed，12.57 秒**。四项限定复核均 ADDRESSED。

复核另发现新 typed-error adapter 会覆盖 backstop cleanup 错误，`eeca9bdf3` 仅修复此优先级。使用实际 legacy starter 的六组合矩阵先出现 2 failed / 4 passed，修复后主 agent 对 adapter/lifespan 复跑 **9 passed**，真实 handoff 子集 **13 passed、8 deselected**。第二轮限定复核 ADDRESSED，无新问题，Task 3 gate 通过。未对未修改管线重复运行全量，整体里程碑留给 Task 4。

## 最终验收矩阵

| 合同 | 必须获得的最终证据 | 状态 |
|---|---|---|
| 默认单机兼容与配置拒绝 | runtime/config 测试、旧测试回归 | 待实施验证 |
| 同 workspace 真并行、共享实体不丢来源 | 独立进程 + 真实 PG/HugeGraph + 核心 pipeline | 待实施验证 |
| 领取先于修复、分页/丢通知/FAILED 人工重试 | pipeline 竞争与重启测试 | 待实施验证 |
| 持久 pending 与异常拒绝 | 原语故障测试 + 实际后端 ACK 丢失/kill | 原语首轮已跑，端到端待验证 |
| 全 SDK/API/后台/维护入口 | 入口矩阵与鉴权/互斥/取消测试 | 待实施验证 |
| 跨存储可恢复进度和 attribution anchors | 图/向量/KV/status 断点失败与受控重放 | 待实施验证 |
| 运维迁移、诊断恢复及部署 | CLI、schema drift、Helm 渲染与 runbook | 原语首轮已跑，完整部署待验证 |
| 整体质量门 | 独立最终 review、Ruff/format、OpenSpec strict、全量里程碑 | 未完成 |
