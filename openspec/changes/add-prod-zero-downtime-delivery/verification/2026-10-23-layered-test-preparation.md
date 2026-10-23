# test 分层准备历史与范围纠正（2026-10-23）

## 当前纠正：复用既有test组件，不重建基础服务

用户指出test已有这些组件。执行者将“独立namespace和数据域”擅自扩大为重建物理PG/图/S3，且误写成用户批准要求，属于范围判断错误，与原design第2节允许共享物理服务的合同不符。**该假设和MinIO镜像阻断均已撤回，不再要求用户补镜像。**

正确路径：按现有test实际配置只读定位PG、图与S3服务；核对代码支持的数据域隔离、权限和共享资源余量；候选应用使用新namespace，在获准隔离数据域内验证，不改原test实例/配置/业务数据。确有隔离不足时报告具体问题再评审必要增量，不预设重建组件。qcloud/CLB另验、1000 QPS另行授权、prod/Git边界不变。

下文及JSON原字段保留为此前错误路线的历史，不是当前部署前置。JSON的`scope_correction`记录纠正；本轮只改文档，不连接环境、不创建资源、不做新打包或运行时测试。003快照保留为当时的本地产物，本次工具说明变化后不再声称其匹配当前工作区，后续真正部署前须重新核验实际采用的候选。

## 选择与边界

用户已选择“现有 test 分层验证，CLB 另验”。目标仍是在独立 namespace/数据域验证候选功能、低并发和应用层 4+4；qcloud/CLB 传播和长流保持另用腾讯云非生产环境验证，不改变生产 Transport、原 test 流水线/实例/数据或 prod gate。1000 QPS、生产、Git 发布、扩大 registry 权限及清理不在本次授权内。

**本次仅完成只读资源/镜像准备和本地候选快照工具。没有创建 namespace 或其他资源，没有部署候选、模型调用、业务写入或真实发压；应用层 4+4、CLB、性能目标均未验收。**

## 本次只读结果

- 目标使用显式 test kubeconfig/context，`kube-system` UID 与[前轮预检](2026-10-23-test-preflight.md)一致；不访问 prod。
- `kubectl top nodes` 可读；worker 观测 CPU 为1%–33%、内存为9%–44%。这是瞬时使用率，**不是资源请求余量、调度成功、后端连接或模型额度证明**。
- 拟用 namespace `lightrag-release-lab-20261023` 不存在；没有创建它，也没有 UID。
- 为避免共享 test 业务后端，计划在新 namespace 内运行专用 PG/pgvector、HugeGraph、S3 服务。对少量明确 namespace 的 Pod 镜像引用只读检查未选出可直接复用的专用后端镜像；不据此断言整个集群不存在这些服务。
- 通过公开 registry 只读 manifest 查询解析到 PG/pgvector、HugeGraph 的 linux/amd64 digest，记录于[脱敏 JSON](2026-10-23-layered-test-preparation.json)。没有拉取镜像到集群、验证启动或据此批准版本/容量。HugeGraph 镜像用法参考其[官方快速开始](https://hugegraph.apache.org/versions/1.7/docs/quickstart/hugegraph/hugegraph-server/)，不将文档作为本环境可用性证据。

### 当前阻断：独立 S3 镜像读取未获授权

仓库配置中的 `minio/minio:RELEASE.2025-09-07T16-13-09Z` 在 Docker Hub、Quay 的匿名 token 路径没有 pull grant，manifest 请求为401/UNAUTHORIZED。对当前镜像仓库相同 repository 的只读查询也为401；仅在内存使用原 test pull Secret 引用，未保存/输出/导入/变更其值。

**401不能证明该 tag 不存在、镜像被删除、原应用拉取失败或凭据已过期。** 这里只确认当前未取得该 repository 的 manifest 读取授权，不能固定可信的独立 S3 镜像。没有继续猜 repository、扩大 registry 权限、修改原 Secret、回退旧版镜像或用 mock S3 绕过。

已向用户请求：test 可拉取的 S3/MinIO 镜像地址（最好带 digest），或者现成的独立 test S3 数据域与凭据引用名称；不在聊天中提供密钥、不寻求 prod 权限。前置不足时，远端创建和部署暂停，仅完成无外部副作用的本地准备。

## 已完成的本地候选快照工具

新增：

- `scripts/performance/candidate_bundle.py`：本地构建/核验 API 与 CLI。
- `tests/performance/test_candidate_bundle.py`：15项行为回归。
- `scripts/performance/README.md`：test分层、快照用法及证据边界。

行为：

1. 只读取声明的源码根与 `pyproject.toml`，不读取 `.secrets/*`、`.env` 或访问网络/集群；拒绝 symlink、凭据类文件名、未知文件类型、缺核心包和超出大小/文件数上限的输入。
2. 必须显式给出基础镜像 digest 和所据 HEAD；生成可复现 archive、逐文件/归档/512 KiB分片 hash，写前复读检测观察到的源码变化，写后核对 tar 文件集合/内容及分片重组。
3. 只允许仓库内新输出目录，目录0700、文件0600；不覆盖或自动删除旧尝试。CLI只输出非敏感身份摘要，没有上传、解包到运行实例或部署能力。
4. 固定 `source_kind=working_tree_snapshot`、`production_eligible=false`。HEAD只是参考，不把dirty字节冒充commit或已构建镜像；基础镜像digest不能证明依赖兼容、挂载实际生效或候选已运行。只读源码仍须审查是否内嵌凭据，双读不是文件系统原子快照。

实际最终本地产物为 `temp/prod-delivery-implementation/lab-candidate-20261023-003/`：419个文件、5,184,906字节压缩归档、10个分片。源清单SHA为 `ec6fe9c61c541040b57afb6cc2676a9443a44501065f07eaafeb9b8733ce1adc`，归档SHA为 `700ec5b3be26c2c863527e33084230915b859ef654687929a697fc49938a1368`；归档/分片与当时工作区逐文件hash已核对。

`001/002` 保留为本地中间快照，未上传、未部署；JSON中的初次146项/002记录保留为历史，当前依据为 `follow_up_validation` 中147项与003身份。后续源码改变必须重建新快照，不能把本记录套到新字节。

## 本地验证

- 初始10项回归 RED：工具不存在。实现后修正测试对错误大小写的匹配，不改变拒绝语义；另分别观察到同长度并发改写、缺核心包、带协议URI冒充镜像引用的回归失败，再实现对应拒绝。
- 最终命令：`PYTHON=.venv/bin/python PYTHON_DOTENV_DISABLED=1 ./scripts/test.sh tests/performance tests/ci/test_prod_workflows.py tests/ci/test_prod_inputs.py -q --tb=short`：**147 passed，9.72s**，包括15项新快照测试。没有运行真实PG/前端/全后端或真实负载；历史9420/123/257不替代本次结果。
- 两个新增Python文件的Ruff check/format检查通过；实际仓库打包、分片/归档/清单核验及当前工作区逐文件hash核对通过。OpenSpec strict与记录/链接/边界核验另见本轮账本，不作为远端验收。
- 最终OpenSpec strict、git diff --check、191个相对链接、53任务/57场景及1611既有文件hash边界通过；原运行代码/CI/部署、参数底稿与来源/现场台账、HEAD/branch及关闭的prod gate保持不变。新代码仅为本地打包工具及测试，原有用户未提交改动保留。

## 下一步与保留项

1. 取得可核验的独立test S3镜像或数据域，重新完成目标/原test状态、资源请求/余量、各后端镜像及隔离前置检查；不因本地快照已准备就跳过复核。
2. 在新namespace内准备专用数据服务/受限runtime与operator身份、持久证据、显式迁移/bootstrap/enroll；只操作新数据域，不用原test恢复脚本。
3. 实现并验证test专用入口/Transport适配，不改生产校验；核验固定基础镜像、只读源码及依赖兼容后，先起一个候选做功能/指标基线，再进行获准的4+4低载协议验收。
4. 冻结有限请求/在途/时长/写fixture/后台尾部预算与停止责任，异常立即暂停；原参数底稿未知项仍空，1000 QPS容量压测及qcloud/CLB专项另行授权/接入。

主任务仍 **41/53完成、12未完成**。8.3、8.6、10.1/10.2/10.8等不勾选；原参数、12类整体批准、来源/现场台账不变。当前无已受理命令、发布owner或本run业务尾部，不执行abort/recover/清理，不提交/push/tag。
