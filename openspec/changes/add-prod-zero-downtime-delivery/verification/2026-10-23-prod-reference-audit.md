# prod 账号、域名、云侧 TLS 引用只读核对

日期：2026-10-23。基于已推送源码 `1664db64b00fe6910fbdfa9ae4e51515db819a16`；本轮新增 HTTP 后端约束仅在本地，未提交推送或应用到集群。

## 结论

**无需用户重发已有密码，也不需要 Kubernetes TLS Secret。** 已有 PG/图/模型/S3 输入、镜像拉取引用和 CLB 可作为复用候选；独立运行权限、LightRAG 正式域名和外部入口证书/观测权限仍缺可验证证据，生产 gate 保持关闭。

用户明确禁止应用交付在 K8s 内做 TLS。TLS 终止及证书管理在云侧 EdgeOne/CLB，K8s Service/Pod 只承接 HTTP；不创建/复制 TLS Secret，不保存/挂载证书或私钥，不在应用 Pod/Ingress 控制器启用 TLS。既有其他业务及控制面 TLS 不作任何变更。

## 实际核对与复用边界

| 项目 | 本次实际证据 | 复用判定 |
| --- | --- | --- |
| prod 输入 | 仅内存解析 `.secrets/prod.secrets`；PG 用户/库均 `lightrag`，图用户/图均 `lightrag001`、graphspace `DEFAULT` | 已有账密无需重发；本轮未连接数据库或重验权限，不能把可认证身份当作最小 runtime |
| API key 别名 | prod 文件内仅有 `lightrag_test_api_key` 这一历史名称；未读 test 文件 | 可在 prod 配置中显式选择这个别名，不是复制 test 密钥，不改源文件 |
| 应用/运维引用 | 全集群 Deployment/StatefulSet 未发现 LightRAG；208项 Secret 服务端 Table 元数据无 LightRAG 命名项；`prod` 仅 `lfun` 和两份 Helm 发布记录，无 Opaque 凭据 | 未发现可引用的独立 runtime/operator；不读取或挪用其他应用/控制器凭据。不能仅据 K8s 记录断言 PG/图服务端没有预置角色 |
| 拉取镜像 | 现有三个 prod 工作负载均引用 `prod/lfun`，该 Secret 类型 `kubernetes.io/dockercfg`，引用存在 | 继续使用原引用，不复制或导入新 pull 密钥；候选 digest 的最终拉取仍需部署验收 |
| CLB/路由 | 21份 Ingress，prod 现有三个域名均在使用同一 `lb-ao2qcdid`；无 LightRAG 路由；6份 TkeServiceConfig 均声明 HTTP/80 | 既有 CLB 为复用候选，不占用/替换其他应用域名或路径；HTTPS/direct 能力仍未证实 |
| 公网证书 | 对既有 `exam.eplus2.cn` 做一次系统信任与 hostname 验证的 TLS 握手，TLSv1.3，SAN `*.eplus2.cn` / `eplus2.cn`，有效期至2027-01-02 15:26:07北京时间；无业务HTTP请求 | 证明既有公网通配证书有效，不证明可作为 CLB 证书引用或可导出私钥 |
| DNS/终止位置 | `exam.eplus2.cn` CNAME 为 `exam.eplus2.cn.eo.dnse2.com.`，解析地址不匹配 Ingress 地址；`rag.eplus2.cn`、`lightrag.eplus2.cn` 当前未解析 | 推断公网链路包含 EO，不将该推断当作实际回源配置。两个候选域名尚未接入，需确认正式域名/DNS |
| K8s TLS 引用 | 全集群类型筛选仅发现 `eduplus/workbench-apisix-client-tls`；仅解析公开 `tls.crt`，SAN是内部服务名，EKU只有clientAuth | 不是公网服务端证书，不复制/引用/挂载，也不要求补新 TLS Secret；未提取或解码私钥 |
| CLB 只读身份 | 仅用 prod 清单明确的 `TENCENT_CLOUD_SECRET_ID/KEY` 调用一次 `DescribeListeners`，返回 `UnauthorizedOperation`；身份/策略名称为COS对象用途 | 不自动重试，不换其他业务或控制器身份绕过，不扩展应用S3身份权限；缺可用的独立入口观测引用 |

关于 EO 的判断仅为推断：腾讯云[官方DNS接口示例](https://cloud.tencent.com/document/product/1552/80716)使用 `*.eo.dnse2.com`；本次未调用 EO 站点/回源 API，实际站点、回源协议、证书来源和流式参数仍未知。当前生产发布器要求经过核验的 CLB HTTPS/direct profile，不能仅凭公网证书或 HTTP/80 配置将其宣称已满足，也不自动放宽为任意公网 HTTP。

## 最小补充清单（仅本次账号/域名/入口范围）

1. **独立 runtime 身份的现有引用，或由运维预置。** 优先复用清单中已有账号作为迁移身份候选，经权限复核后使用；仅补 PG/图独立最小运行身份及其引用，不新建基础服务/数据库，不扩大原 test 账号权限。若角色已在服务端存在，只需指出现有配置/Secret引用，勿在聊天贴密码。
2. **确认正式域名及其接入。** 可选一个新的专用子域名，例如尚未解析的 `rag.eplus2.cn`；不能从其他已在使用的应用域名推定 LightRAG 域名，不能覆盖现有路由。DNS/云入口修改本轮未获执行，也未进行。
3. **可用的云侧入口只读身份或现有配置引用。** 对既有 CLB 至少需要发布器使用的 `DescribeListeners`、`DescribeTargets`、`DescribeTargetHealth`；由此自行核对listener/location/证书ID，不要求先新建 TLS Secret。若沿用 EO 外层入口，还需能核对既有站点/域名证书与回源配置的引用或只读证据；不得把 COS/应用凭据改成发布控制身份。

这不是全部上线前置已完成的清单。收到上述引用后，仍需经真实授权核验最小权限、显式 migration/register/enroll、S3 ACL/读写、工作路径/恢复语义、4/8 Pod及后端/模型预算与入口连续性。1000QPS及既定SLO未验收，快速上线不等于批准降低目标或免除门禁。

## 已完成的本地防误配整改

- 移除未被渲染使用的 `target.tls_secret` 必填字段和 JSON Schema 属性，旧字段直接拒绝，不用假引用填门禁。
- slot 渲染前拒绝应用 SSL 或证书/私钥文件参数，Pod 显式 `SSL=false`；应用启动 profile 再次拒绝相关参数。
- 渲染继续为 HTTP/9621、HTTP readiness/startup，无 TLS Secret或证书卷。Ingress 的云侧证书ID仅用于外部 CLB 的路由控制，不包含证书或私钥，不在 K8s 终止 TLS。
- 保留云侧 HTTPS、证书ID、force-close和真实路由验收；没有证书/入口证据仍拒绝。test/local原 SSL行为、其他业务及控制面不变。

TDD先观察到 **10 failed / 1 passed**，再 **11 passed**；之后补外部云证书/HTTPS拒绝回归。最终相关目录 **405 passed / 60 skipped / 2既有弃用warning**，命令见[机器记录](2026-10-23-prod-reference-audit.json)。集成标记仍默认跳过，未调用真实PG/图/S3/模型。历史9661通过属于前一提交，不冒充本轮新增代码的全套验证。

## 现场与记录边界

- prod 三个工作负载、21份Ingress及6份ServiceConfig的UID/spec和路由引用前后一致；创建Pod/Job、exec/port-forward、变更、切流、业务HTTP、PG/图/S3请求均0。
- 原 test 和两个候选未访问或修改；未重启/recover/重放、导入密钥、推送新提交或打tag。所有secret处理限进程内，未输出或保存凭据/证书私钥；本报告只记录非敏感身份与引用。
- 新采集器最初有一次本地参数断言失败（未发请求）和一次全量Secret列举退出1；后者未保留真实诊断，不能沿用被wrapper参数干扰的“Timeout”分类，也不把失败当无Secret。随后服务端Table元数据、类型筛选和目标引用GET完成核对，未重试那条全量请求；CLB拒绝仅一次，不换身份。
- 原始脱敏投影、失败/成功本地测试日志保留于本仓库ignored的`temp/prod-reference-audit-20261023-184005/`。本地改动尚未进入正式镜像或部署；生产静态gate仍false。
- OpenSpec **47/59完成、原12项未完成**不变；新增1个本地安全场景后共63场景，不签署生产或性能验收。
