# test 隔离验收前只读预检（2026-10-23）

## 授权与本次结论

用户批准先只读预检 test，满足条件后在**独立 namespace 和数据域**部署候选，低负载验证功能、并发及 4+4 蓝绿；异常立即暂停。原 test 流水线、实例和数据不变，不操作 prod、不导入 prod 密钥、不提交/push/tag；1000 QPS 压测另行授权。后续“按顺序推进、逐步改进直到指标”没有取消这些边界。

**本次完成了现有 test 的逐 Pod 业务接口检查，但没有通过隔离演练的全部执行门禁。候选未部署，未发送模型或业务写请求，性能指标仍未验收。** 当前入口观测与既定 qcloud/CLB 生产验收路径不一致，部署/发布阶段已暂停，等待确认分层测试还是腾讯云非生产目标。

## 实际目标与方法

- 仅使用 `/Users/minwang/.kube/test-config`、显式 `context=default`，通过 `kubectl-env`；未读取 prod kubeconfig、prod 清单或账户。
- `kube-system` UID：`d693583f-5864-4ee6-ab4f-c31302ffcc01`。节点 kubelet 为 `v1.32.13+rke2r1`，11 个节点 Ready，观测时 Memory/Disk/PID Pressure 均为 false。**这不是可调度余量、8 Pod 预算或共享服务非干扰证明**；尚未核算请求/余量。
- 各 Pod 使用绑定 `127.0.0.1` 的临时 port-forward，逐个认证 GET `/health`、`/documents/pipeline_status`；每个 namespace 另 GET 一次 `/documents/status_counts`。没有通过 Service 随机抽样或查询生成接口代替逐 Pod 检查。
- 仅为认证 GET 在内存读取原 test 的 Kubernetes `lightrag-runtime` Secret/API key；未导入、修改、复制到新 namespace 或保存/输出密钥，未读取 `.secrets/test.secrets` 或 `.secrets/prod.secrets`。证据只记录 Secret UID/resourceVersion 等允许公开的元数据。
- 所有本次转发客户端均已关闭，没有创建 namespace、Job、Pod、数据库、图、bucket、Secret、RBAC 或 Ingress。未执行 exec、重启、recover、数据库变更、发压或故障注入。

## 逐 Pod 实测摘要

最终业务观测窗口：UTC `2026-10-22T16:36:17.395186Z` 至 `16:36:24.630464Z`，即北京时间 **2026-10-23 00:36:17–00:36:24**，详见 [JSON 原始脱敏记录](2026-10-23-test-preflight-final.json)。这是短窗口点检，**不是 5 分钟稳定性、持续查询连续性或性能测试**。

| 项目 | 结果 |
| --- | --- |
| Pod Running/Ready、重启计数 | 10/10 Ready，重启计数均 0 |
| 逐 Pod `/health` | 10/10 HTTP 200 |
| 逐 Pod `/documents/pipeline_status` | 10/10 HTTP 200 |
| polling | 10/10 `polling_active=true`，无 `polling_error`，`discovery_stopped=false` |
| 协调拒绝/遗留状态 | 观测均无 fence、recovery_required、orphan、claim、待重试或 paused |
| generation | test-01/02/03/04/05 分别为 1/1/2/1/1 |
| 原资源非变更复核 | 5/5 Deployment UID/generation/spec、原 Secret UID/resourceVersion 及 10 个 Pod UID 在前后复核中不变 |

文档计数仅见 processed：test-01 为 5、test-03 为 1、test-04 为 3，其余为空；无 pending/parsing/analyzing/processing/preprocessed，未发现待处理文档。合法 `all` 汇总已与分状态总和核对。

test-04 的一次 `active_operations=1`，其余读数为 0；聚合 API 不提供该 operation 的 kind，本次**没有将它推断为空闲 polling，也没有宣称全部 workspace 无活跃操作**。没有通过完整历史 inspect、业务试写、停 polling 或额外数据库操作制造“空闲”。pending mutation、资源锁和独立数据域 ACL 等未在本次业务 API 点检中核验，仍不能签署候选执行前的完整安全前置。

## 阻断与下一步

实际登记的 IngressClass 只有 `nginx`，controller 为 `k8s.io/ingress-nginx`。本次没有取得可核验的 qcloud HTTPS/直连 Pod 测试目标、listener/location、只读 CLB 后端观测及连接保持证据。**仅未登记 qcloud 不等于证明任何未登记 controller 都不存在**，但现有证据不足以运行生产入口验收。

生产配置的 `lightrag_delivery/config.py::validate_delivery_profile` 明确要求 qcloud/HTTPS/direct，真实 `ProductionTransport` 与混合演练依赖该合同。不能伪造 CLB 参数、跳过真实入口观测、把 nginx 测试称作 qcloud 通过，或为跑通演练放宽生产门禁。

待用户确认测试路径：

1. **建议分层**：现有 test 的独立 namespace/数据域先做功能、低并发与应用层 4+4 协议验证；须在同一 OpenSpec 明确测试专用入口/适配及证据边界，保持生产校验与原 test 部署不变。qcloud/CLB 的传播、长流保持和真实生产入口切流留给腾讯云**非生产**同构环境专项验收。
2. 或提供腾讯云**非生产**隔离目标及只读接入信息，在那里完成端到端 4+4；这不是访问 prod 的许可，不需要在聊天中提供凭据。

选定路径后，仍需核验独立 PG/图/S3/存储域与 ACL、资源余量、专用身份、当前 dirty 候选源码/镜像的可审计交付、最小 seed、有限低载/尾部预算和停止处置。上述现场未知项继续留空，不把本次授权改写为容量/故障/生产/清理的整体批准。优先完成低载基线，再按同数据/负载的阶段耗时证据优化；1000 QPS 的阶梯/持续容量测试另行授权后才能评价此前 SLO。

## 证据与观察器问题

- [首次观察器记录](2026-10-23-test-preflight.json)：错误假定 `app=lightrag` 导致匹配 0 Pod；是本地观察器标签错误，**不是原 test 副本数变化**。
- [第二次观察器记录](2026-10-23-test-preflight-business.json)：首 Pod 的 health/协调 API 均 200，观察器未接受已有合同的 `status_counts.all` 汇总而暂停；**不是服务端文档状态异常**。
- [最终脱敏记录](2026-10-23-test-preflight-final.json)：改用 live Deployment selector、接受并核对 `all`（含 4 个纯本地校验断言），完成 10 Pod 点检及原资源前后复核；因未满足真实 qcloud 入口前置返回非就绪。前两次记录保留，不覆盖或伪装为成功。

本轮只新增预检证据并同步 OpenSpec 授权/状态，不改变应用、流水线或部署实现，不运行后端/前端单测来冒充远端验收。8.3/8.6/10.1/10.2/10.8 等仍未完成，主任务维持 **41/53 完成、12 未完成**；51 写入口、26 故障、容量及发布 phase 的现场台账仍为 not_run。参数底稿的整体批准和未知项不因本轮点检而改写。

## 本地记录核验

- `openspec validate add-prod-zero-downtime-delivery --strict`、`git diff --check` 通过。
- 脱敏 JSON 投影与报告核对：10 个唯一 Pod UID、20 次 health/协调 HTTP 200、5 份状态计数、polling/拒绝状态、前后资源不变及未部署/未达标标志一致；保留 test-04 的非零 active 读数及观察器失败尝试，不将异常记录改成成功。
- 对照文档编辑前的 1611 个既有文件 hash，仅 5 份指定 OpenSpec 状态材料变化；应用/测试/工具/CI/部署、参数底稿、来源/执行台账和关闭的 prod gate 不变，HEAD/branch 不变。新增材料为本报告及 3 份脱敏 JSON，不含凭据字段或业务正文。
- 核对 186 个相对文件链接、53 项主任务与 57 个 capability 场景；不将本轮元数据/HTTP 点检升级为后台指标、候选功能、持续性、质量或性能验收。
