# prod TKE 内三组件有界只读检查完成（2026-10-23）

用户说明数据库最新密码已修改并要求重试，沿用此前授权：仅在目标 TKE 对本次 PG 探针使用非 TLS，复用 `prod/lfun`、延长启动等待，检查 PG/图/S3 后删除临时 Pod，异常即停。不修改后端/应用配置、不部署应用、不切流、不发压、不导入新密钥，不操作原 test、两个候选或 Git 发布。

**结论：本轮 PG 元数据、图版本/schema/常量查询、S3 HEAD/有界列举三项检查均完成。它们证明所测只读访问可用，不代表完整权限隔离、应用功能、4+4或生产放行。** 历史密码错误/TLS拒绝记录保留，不覆盖为已成功。

## 1. 目标、凭据与临时资源

- 固定目标 TKE `cls-px392o6p`，核对 `kube-system` UID 和既有 `prod` namespace。仅复用现成 `prod/lfun` 引用，不读取 Kubernetes Secret payload、创建或复制密钥。
- 只创建 `prod/lightrag-ro-diag-20261023-05`，Pod UID `4fd395fe-b611-4dfb-ab95-563e55031a2f`；该 UUID 仅为诊断对象身份，不是 prod 发布版本。
- 使用此前固定镜像 digest，实际 imageID 一致；覆盖应用入口为 Python sleep，无业务端口/卷/token，无 Service selector 匹配。非 root/只读根、有界临时卷/资源、600秒启动窗口与900秒Pod有效期保持。16:46:02（北京时间）成功启动，restart=0。
- 验证 asyncpg 0.31.0、boto3 1.40.61、httpx 0.28.1。新进程重新读取当前 `.secrets/prod.secrets`，不复用前轮内存或旧密码；未读取 test 清单。必要字段仅通过标准输入进入探针内存，不在对象、env、命令参数、远端文件或结果中保存密码/密钥。

## 2. PostgreSQL：认证与9条元数据只读查询完成

一次显式 `ssl=False` 连接，目标解析1个 RFC1918 地址；建立已认证会话，实际 `pg_stat_ssl.ssl=false`。连接启动默认只读，并在 `readonly=True / repeatable_read` 事务执行9条 SELECT，`transaction_read_only=on`；语句超时5秒、锁等待1秒，无自动重试、DDL、数据写入、迁移、recover或其他数据库连接。

| 元数据 | 本轮实际值 |
| --- | --- |
| PostgreSQL / pgvector | 17.6 / 0.8.0 |
| max_connections / superuser_reserved_connections | 2048 / 10，reserved_connections=0 |
| pg_stat_activity 可见行数 | 总343、目标库1；受角色可见性约束，不是完整活跃状态审计 |
| 当前事务 | read_only=on，standby=false |
| 当前角色标志 | 非superuser，无CREATEDB/CREATEROLE/replication/bypassrls；rolconnlimit=-1 |
| 当前库/ public schema CREATE | 均true |
| 所查 schema | 仅public，未出现lightrag_coordination/lightrag_release |
| 匹配LightRAG表/发布表权限行 | 0 / 0 |
| 目录显示其他库CONNECT权限数量 | 28，未实际连接或读取其他库 |

新密码对应的本次认证已成功。连接快照不能当8Pod容量批准：不能把2048减343直接作为可分配预算；active/idle字段受可见性限制，也不是无写入、无未知writer或安全排空证明。

上线前还需显式准备/核验协调与发布元数据，并确认 runtime/operator 权限分离。当前角色具有本库和public CREATE，不能直接当已验证的最小runtime身份；其他库CONNECT也不等于已能读取其数据。**本轮不创建schema/角色、不扩权、不初始化。** 单次非TLS诊断授权不自动变更应用长期传输策略。

## 3. HugeGraph：版本、schema读取与常量Gremlin成功

- 目标解析1个 RFC1918 地址，沿用现有HTTP端点及配置身份；GET `/versions` 和目标graphspace/graph的`/schema`均200，core为1.7.0。
- 采用当前 HugeGraphClient 实际 REST `/gremlin` 路径与 graph/g别名，发送仅常量`1`的只读表达式；HTTP200、Gremlin status200、结果`[1]`。不遍历实体/关系或读取业务正文；本轮图请求合计3次，无重试/变更。
- 七项LightRAG定义均未出现：lightrag_scope/name/data、entity/relation标签及两项索引。因此仅确认接口/既有目标/常量执行路径可用，不签署LightRAG图存储已初始化、数据兼容或业务查询通过。
- 未调用匿名/错误身份或跨scope拒绝探针，不能凭配置身份请求200证明鉴权强制开启或scope ACL正确；没有自动建图/补schema。

## 4. S3：TKE内HTTPS元数据访问成功

| 请求 | 实际结果 |
| --- | --- |
| HeadBucket | 200 |
| ListObjectsV2(MaxKeys=1) | 200，KeyCount=0、IsTruncated=false |
| 请求目标域名解析 | 1个地址，is_private=true |

仅2次SDK HTTP发送，HTTPS默认验证未关闭，无重试；不保存对象键/正文，不请求PUT/DELETE、对象内容或已知无关的ACL/policy管理API。KeyCount=0仅是此身份/此时的列举结果，不是整个bucket绝对为空或IAM隔离证明。

这比前轮本机S3检查增加了真实TKE内观察点与私网地址解析证据；未抓包、查VPC流日志或独立审计路由，因此不扩大为完整网络路径审计。对象GET权限、指定prefix隔离、写/删、原件读回和持久化仍需可信策略证据或另获准的隔离对象验证，不能用HEAD/List200代替。

## 5. 清理、非干扰与边界

- 16:46:20开始正常删除，16:46:31确认删除；16:47:20再次确认该Pod不存在。创建=1、删除=1，无剩余诊断资源，未force删除或自动重建。
- 本轮前后namespace内原27个Pod、3个Deployment/StatefulSet，UID/spec hash一致。原test及两个候选未访问，不声称重新验证它们实时健康；没有应用部署、切流、发压、后端数据/配置变更、新密钥导入或Git发布。
- 9条SELECT、3次图HTTP、2次S3 HTTP均完成，探针记录backend_mutations=0；事务控制/连接本身不冒充“未发生请求”，也不将只读结果冒充写权限验收。

完整脱敏结果见[JSON](2026-10-23-prod-incluster-readonly-completed.json)。固定无凭据脚本/manifest、前后投影及清理证据在仓库私有 `temp/prod-diag-20261023-05/`，不是正式交付镜像、版本或生产就绪凭证。

本轮完成9.2内的有限只读子检查，**不勾整项9.2或其他真实验收任务**：初始化/最小权限、S3隔离、入口切流、4+4资源/连接预算及功能/性能仍未验。保持41/53完成、12待办、57场景、196个未知参数与12类整体批准原样。只更新证据文档和临时运维脚本，没有修改应用/测试/流水线/部署模板，也未重跑无关单测。
