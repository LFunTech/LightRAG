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

### 部署与独立进程验收（Task 4，实现侧定向验证）

新增 `python -m lightrag.distributed bootstrap`，复用 API 的完整配置构造器和 pre-init maintenance。Helm 新增显式 distributed Deployment/RWX/Secret/非 root profile、existingClaim 和独立维护 Job；Job 要求 replicas=0 与 stopped/quiescent 运维确认，无迁移 hook/自动 retry。默认本地 Deployment 单副本/RWO/30 秒 Kubernetes 终止宽限不变。完整升级、离线审计修复、回退与三确认恢复见 `docs/DistributedDeployment.md`。

实现侧执行真实 PG + HugeGraph：

```bash
set -a
source .superpowers/sdd/tasks/test-env
export LIGHTRAG_TEST_COORDINATION_DSN="$LIGHTRAG_COORDINATION_DSN"
set +a
PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh \
  tests/distributed tests/api/routes/test_distributed_controls.py \
  tests/setup/test_distributed_chart.py --run-integration -q
```

**190 passed，34.72 秒**（最终真实定向运行，使用独立 `--basetemp=/tmp/lightrag-task4-final` 保存证据）。其中 chart **12 项**；实际独立进程复跑和详细证据单列于 Task 4 报告。每个业务进程使用独立 OS PID、独立 Manager PID、独立 coordinator owner/pool。真实 pipeline 对不同文档的 HugeGraph HTTP 请求起止时间重叠；共享实体/关系保留两个不同真实来源与权重下界，重复投递不重复解析或增加证据；本机 mailbox 不互通仍通过周期扫描发现任务；全局 pause 不被 polling 撤销；FAILED 一次性 retry 在接受者退出后仍有效且同 request ID 不增加尝试。

故障在真实 Atlas 图写已返回后、durable ACK 前注入：实际 SIGKILL 或丢响应，保留 graph 已存在/vector 未落地/status 未完成及 full_entities/full_relations anchors。测试等待全部已发送 HugeGraph 请求完成、阻止 fault 后新请求，停止全部 owned writer，再从 PG `pg_stat_activity` 确认标记过的业务 sessions 消失；重建 coordinator inspect 与新进程启动仍拒绝，无 TTL 接管。执行真实 recover CLI 后，新进程通过原生产 purge 删除剩余贡献并收敛，历史/恢复审计仍在。维护竞争在 admission 前不创建标记文件或执行业务写。旧 permit、SQL ACK 丢失和 HTTP 后台 lifetime 矩阵继续复用 Task 1–3 的真实/单元回归，不声称每种已有矩阵均改成子进程。

默认 mock/mirror：

```bash
PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh \
  tests/distributed tests/setup tests/api/config \
  tests/api/test_distributed_lifespan.py tests/test_docstring_budget.py -q
```

**602 passed，78 skipped，31.26 秒**（最终 mirror）。测试实际覆盖生产 bootstrap CLI 的环境构造、重复初始化、dotenv 不覆盖 Secret 环境值；未发出模型请求。Ruff/format、Helm lint（默认/分布式）、三组真实 template/YAML 解析和 strict OpenSpec 通过，精确命令与 TDD 红绿记录见 Task 4 报告。

未执行 Kubernetes 部署、RWX provider 验证、实际 Secret/网络策略/节点故障或 Gunicorn preload 验收；chart 支持路径固定 WORKERS=1、跨 Pod 扩容。未运行额外镜像大构建、未修改 9621 服务、未删共享业务图/schema/协调历史。全量里程碑和独立最终 review 由父 agent 执行，4.3/4.4 仍未勾选；已有两项 401/403 path-prefix 基线不在本次 mirror 路径，没有宣称修复或隐藏它们。

## 最终验收矩阵

| 合同 | 已有证据 | 状态 |
|---|---|---|
| 默认单机兼容与配置拒绝 | Task 1–3 mirror + Task 4 CLI/Helm 默认与拒绝矩阵 | 定向验证通过，待全量门 |
| 同 workspace 真并行、共享实体不丢来源 | 独立进程/Manager + 真实 PG/HugeGraph + 生产 pipeline + HTTP 时间交叠 | Task 4 已验证 |
| 领取先于修复、分页/丢通知/FAILED 人工重试 | 既有调度矩阵 + 独立进程重复解析计数/周期发现/重启 retry | 已验证 |
| 持久 pending 与异常拒绝 | 既有 SQL ACK/permit 矩阵 + 独立业务进程实际 HG ACK 丢失/SIGKILL | 已验证 |
| 全 SDK/API/后台/维护入口 | Task 2–3 已审查矩阵 + 独立维护竞争 + 实际 bootstrap CLI | 定向通过，待最终 review |
| 跨存储可恢复进度和 attribution anchors | 实际图已提交、向量未提交、锚点保留 → 审计 recover → 原生产 purge | Task 4 已验证 |
| 运维迁移、诊断恢复及部署 | 实际 CLI、Helm lint/template/YAML、runbook/离线操作路径 | 实现侧通过；未实际部署集群 |
| 整体质量门 | Ruff/format、OpenSpec strict、定向 mirror | 全量里程碑与独立最终 review 待父 agent |
