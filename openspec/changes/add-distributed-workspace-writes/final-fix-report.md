# 最终修复波报告

> 本文件保留实现者提交时的原始报告。后续独立复审已全部通过，当前验收状态和主 agent 复跑结果以 [verification.md](verification.md) 为准；下文临时目录是历史执行上下文，不是运行依赖。

## 状态与范围

- 状态：四项指定问题均已修复并完成定向红绿与 mirror 验证，等待 root 独立最终复审；不声明已获合并批准。
- BASE：`f9f67dd3cb1a48fcfc1d426c043fd8c44fefd5ff`。
- HEAD（完整实现、测试及合同文档的已验证提交）：`03a5496c5fe9c2342025eaa4a41aa3a1a208ec95`。
- 分支：`feat/distributed-workspace-writes`。本报告在上述 HEAD 后以单独报告提交保存；没有后续生产代码修改。
- 本轮只处理 FR-I1、FR-I2、FR-M1、T4-M1；没有新代理、worktree、push、merge、PR、服务重启、配置/Secret 修改或 Kubernetes 操作。`tasks.md` 的 4.4 保持未勾选，最终 verification/task bookkeeping 由 root 负责。

## FR-I1：retry 接受与 resume 原子提交

### 根因与修复

实际 `_PipelineMixin.apipeline_request_retry()` 先提交 `PipelineControl.request_retry()`，再单独调用 `resume()`。已经 paused 的工作区在第一笔提交后接受进程退出，会留下不能被普通轮询服务的 pending intent；重复 request ID 的第二次 resume 还会覆盖后来提交的 pause。

- `lightrag/distributed/control.py`：使用 `INSERT ... ON CONFLICT DO NOTHING RETURNING request_id,state,created_at` 区分新接受和重复投递。只有新插入行才在**同一个已持有 workspace 行锁的协调事务**里执行 `paused=false`。重复 ID 只读取原持久状态，不重置时间、请求、目标或 `cancel_epoch`，不追加 attempt，不 resume。
- `lightrag/pipeline.py`：删除 SDK 中提交后的独立 `resume()`，避免后续 pause 被第二笔事务覆盖。
- `docs/design/DistributedPipelineContract.md`：写清单一提交点、提交后响应前退出、后来的 pause 获胜、pending/completed ID 的幂等语义、保持 cancel epoch，以及显式新请求/process 与普通 replay/poll 的区别。
- 没有修改 scheduler 的 `resume=False` 行为，没有自动 FAILED retry、TTL、未知业务写接管或自动解除 fence。

### 覆盖与证据

1. `tests/distributed/test_pipeline_control_integration.py::test_retry_resumes_once_and_duplicate_never_reverses_later_pause`：真实独立 coordinator 验证 paused→新 ID→resume，随后 pause→pending ID 重投仍 paused；完成后同 ID 仍 paused 且不新增 pending；新 ID 才可 resume；cancel epoch 不回退。
2. `tests/distributed/test_pipeline_business_integration.py::test_pending_retry_replay_and_poll_preserve_later_pause`：实际 SDK、PG doc-status 和真实 scheduler `process(resume=False)`，验证后续 pause 下 pending intent 留存、FAILED 状态及版本不变；之后显式 process 可以实际处理到 PROCESSED。
3. `tests/distributed/test_process_acceptance.py::test_paused_retry_commit_before_response_survives_acceptor_death`，配合 `tests/distributed/process_worker.py`：spawn 的独立 OS/Manager/pool 进程；接受者被截停在 coordinator **第一笔真实事务提交完成后、control/SDK/pipe 返回前**，父进程确认未收到响应后 SIGKILL。另一进程确认 pending=1、paused=false、无 recovery fence；调用实际轮询 scheduler 而非显式 resume API，执行一次真实 FAILED 重试；完成后 pause→同 ID→poll，模型调用数和失败版本都不增加。
4. 加强后的“第一笔 commit”断点另对 BASE 实现做了单测反证：`1 failed, 5 deselected`，失败为持久 `paused=True`；恢复修复实现后 `1 passed, 5 deselected`。没有仅模拟内存提交或在 SDK 已响应后才关闭进程。

## FR-I2：scan 的 typed admission 错误和后台清理

### 根因与修复

实际 `/documents/scan` 的 distributed 分支仍直接调用旧 `start_reserved_background_task`，把后台 admission 的 typed 异常包装为 generic RuntimeError。修改 `lightrag/api/routers/document_routes.py`，复用现有 `start_background_task(rag, ...)`，不复制或绕过清理/取消/join 逻辑。

- busy 返回 409 + `CoordinationBusyError`；其他 coordination admission 故障返回 503 + 对应异常类型。
- 未接管的后台任务先退出并 join；取消向调用者传播，不伪报 `scanning_started`；已接受的 durable retry intent 不撤回。
- 原有 cleanup-failure 优先级不变，local scan 分支及 `combined_auth` 不变。
- `docs/design/DistributedPipelineContract.md` 同步 typed startup 和“意图已接受、分类未启动”的残留语义。

### 覆盖与证据

1. `tests/api/routes/test_distributed_scan_start.py::test_scan_http_startup_failure_is_typed_and_joined[busy|unavailable|cancel]`：真实 FastAPI 路由、认证、SDK、runtime/background decorator、starter 和 exception handler，仅用测试替身模拟外部 coordinator admission。无 key 的 HTTP 请求仍拒绝且没有接受意图；合法请求实际验证 409/503、取消传播、join 及意图保留。红测 `2 failed, 1 passed`（500≠409、500≠503），绿测 `3 passed`。
2. `tests/api/routes/test_distributed_controls.py::test_scan_admission_http_error_retains_intent_and_joins_child[busy|unavailable|cancel]`：真实 PG/HugeGraph fixture 与独立 peer shared ticket。busy 在真实 exclusive admission 等待后超时；503 在实际 coordinator operation 边界注入 unavailable，不停止服务；取消发生于真实冲突事务已结束的等待点。三种情况均断言后台已 join、没有 scan operation、只有原 peer active ticket、pending retry=1、无文件 mutation，退出 peer 后未 fenced。红测 busy/unavailable 返回 HTTP 500，绿测三个用例通过。
3. 同时复跑 `tests/api/test_distributed_background_start.py` 的六种 child/cleanup 组合，既有 cleanup 优先级全部通过。

## FR-M1：HugeGraph 主指南 local/distributed 恢复边界

`docs/HugeGraphStorage.md` 在并发/pending 和人工恢复标题显式限定 `distributed_writes=false`；保留旧 local Manager 合同，新增 opt-in PG/HG/shared-filesystem profile 和 runtime/deployment 链接。单列 distributed 恢复：fence 跨全部应用进程/Manager 重启保留，先停止 writer、确认在途结束、inspect/audit，再显式 recover；不能反复重启解锁，也不把 distributed CLI 恢复反套到 local 模式。逐段对照现有 runtime/deployment 合同人工验证，未为人类说明文档新增文本匹配测试。

## T4-M1：AUTH_ACCOUNTS 的 TOKEN_SECRET 前提

`docs/DistributedDeployment.md` 补齐：使用 `AUTH_ACCOUNTS` 时，同一 external Secret 必须提供强随机、非默认且所有副本一致的 `TOKEN_SECRET`；API-key-only 不需该配置。核对 `lightrag/api/config.py::validate_auth_configuration` 的现有拒绝条件，未改鉴权、Secret、配置值或启动校验。

## 实际命令与输出

所有命令在仓库根目录执行。真实集成命令的共同前置环境（无内容输出、无文件修改）：

```sh
set -a
source .superpowers/sdd/tasks/test-env
set +a
export LIGHTRAG_TEST_COORDINATION_DSN="$LIGHTRAG_COORDINATION_DSN"
```

每次使用唯一 `/tmp` basetemp；以下 `$(date +%s)` 与实际执行命令一致。完整 stdout/stderr 留在对应日志。

### 有效红→绿（不把测试编写错误当产品红测）

```sh
PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh tests/api/routes/test_distributed_scan_start.py --basetemp=/tmp/lightrag-finalfix-unit-red-$(date +%s)
# 2 failed, 1 passed in 0.42s
# /tmp/lightrag-finalfix-unit-red.log
# 同命令将 unit-red 换为 unit-green：3 passed in 0.27s
# /tmp/lightrag-finalfix-unit-green.log

PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh tests/distributed/test_pipeline_control_integration.py tests/distributed/test_process_acceptance.py tests/distributed/test_pipeline_business_integration.py tests/api/routes/test_distributed_controls.py --run-integration -k 'retry_resumes_once or paused_retry_commit or pending_retry_replay_and_poll or scan_admission_http_error' --basetemp=/tmp/lightrag-finalfix-real-red3-$(date +%s)
# 5 failed, 1 passed, 41 deselected in 3.44s
# /tmp/lightrag-finalfix-real-red3.log
# 修复后同命令将 real-red3 换为 real-green：6 passed, 41 deselected in 3.48s
# /tmp/lightrag-finalfix-real-green.log

PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh tests/distributed/test_process_acceptance.py --run-integration -k paused_retry_commit --basetemp=/tmp/lightrag-finalfix-firstcommit-red-$(date +%s)
# 临时以 git show BASE:<path> 放回 control.py/pipeline.py 的原始内容；事先备份，trap 保证恢复修复内容。
# 加强后的 FIRST-commit 断点：1 failed, 5 deselected in 2.27s，exit=1。
# /tmp/lightrag-finalfix-firstcommit-red.log
# 恢复修复后将 firstcommit-red 换为 firstcommit-green：1 passed, 5 deselected in 2.37s。
# /tmp/lightrag-finalfix-firstcommit-green.log
```

测试编写初期曾修正一个 doc-status 方法名笔误、monkeypatch 清理作用域及取消断点过早导致的 fixture teardown 错误；有效红证据采用修正后的 `real-red3`，无 fixture errors。初次 Ruff 发现测试中未使用的 `client` 重定义，已改为 `_`；最终 Ruff 为干净结果。

### 最终 mirror / real / 文档检查

```sh
PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh tests/pipeline tests/api/routes tests/distributed tests/api/test_distributed_background_start.py --basetemp=/tmp/lightrag-finalfix-mirror-$(date +%s)
# 1626 passed, 108 skipped in 47.98s
# /tmp/lightrag-finalfix-mirror.log

PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh tests/distributed tests/api/routes/test_distributed_controls.py tests/api/routes/test_distributed_scan_start.py tests/api/test_distributed_background_start.py --run-integration --basetemp=/tmp/lightrag-finalfix-real-mirror-$(date +%s)
# 193 passed in 37.82s
# /tmp/lightrag-finalfix-real-mirror.log

PYTHON=/tmp/lightrag-distributed-test-python ./scripts/test.sh tests/test_docstring_budget.py --basetemp=/tmp/lightrag-finalfix-docs-$(date +%s)
# 16 passed in 1.88s
# /tmp/lightrag-finalfix-docs.log
```

Ruff 的精确文件集合（9 个变更 Python 文件）：

```sh
/tmp/lightrag-distributed-test-python -m ruff check lightrag/distributed/control.py lightrag/pipeline.py lightrag/api/routers/document_routes.py tests/distributed/process_worker.py tests/distributed/test_pipeline_control_integration.py tests/distributed/test_pipeline_business_integration.py tests/distributed/test_process_acceptance.py tests/api/routes/test_distributed_controls.py tests/api/routes/test_distributed_scan_start.py
# All checks passed! — /tmp/lightrag-finalfix-ruff.log
/tmp/lightrag-distributed-test-python -m ruff format --check lightrag/distributed/control.py lightrag/pipeline.py lightrag/api/routers/document_routes.py tests/distributed/process_worker.py tests/distributed/test_pipeline_control_integration.py tests/distributed/test_pipeline_business_integration.py tests/distributed/test_process_acceptance.py tests/api/routes/test_distributed_controls.py tests/api/routes/test_distributed_scan_start.py
# 9 files already formatted — /tmp/lightrag-finalfix-format.log
openspec validate add-distributed-workspace-writes --strict
# Change 'add-distributed-workspace-writes' is valid — /tmp/lightrag-finalfix-openspec.log
git diff --check
# 无输出，exit=0；提交前 git diff --cached --check 亦通过。
```

已人工检查完整本轮 diff，没有无关生产变更。上述测试有交集，不将通过数相加冒称独立用例总数。

## 剩余 concerns / 验证边界

- 本轮四项没有剩余已知阻塞；是否关闭最终 review 与 4.4 由 root 决定。
- 未重跑全量。root 给定的前置里程碑 `8813 passed / 360 skipped / 2 known-baseline 401/403 failed` 不由本报告重新背书或伪称全绿；本轮 mirror 及真实子集无失败。
- 外部真实 PG/HugeGraph 集成仅使用已有 dedicated test env、随机 owned workspace/隔离 fixture，保留取证历史；没有修改/删除共享业务 scope、共享 schema、服务配置或密钥。
- 没有实际 Kubernetes、多节点 RWX、镜像构建、Gunicorn preload 或收费模型调用；独立进程证据不是多 Pod 实测。503 场景是在真实调用边界注入 coordination unavailable，而不是停止外部数据库服务。
- 没有修改 durable pending/ACK、来源锚点、关系证据下界、物理资源并行、已有 cleanup 优先级或默认 local 路径。
