# 实际 prod 部署推进与阻断

> 本文保留浏览器配置前的只读调查快照。随后EO域名、免费云证书、专属缓存/流式规则及阿里云CNAME已经实际配置，最新状态见[后续执行记录](2026-10-23-edgeone-browser-delivery.md)。运行/迁移身份、CI入口观测、首发初始化/串接及真实验收仍未完成；应用尚未部署。

日期：2026-10-23（北京时间）。用户再次明确要求实际部署，不以镜像构建成功代替上线；随后确认正式域名 **`llm-rag.eplus2.cn`**、公网入口沿用 **EdgeOne**，凭据来源仍仅为 `.secrets/prod.secrets`。

## 当前结果

**尚未部署应用、未初始化业务结构、未切流。** 正式版本 `v1.5.8` / commit `b79ba11e438076e58ebf05e06b4f436682a29e62` 的[流水线 #75](https://woodpecker.f123.pub/repos/57/pipeline/75)成功，但没有执行 `deploy-prod`。

本次从 registry 重新核对 linux/amd64 digest，并在目标 TKE 使用 `prod/lfun` 拉取运行成功：

```text
docker-hub.f123.pub/lfun/lightrag@sha256:9a01084b132ac015f6187239c8b99a541a8de2f3cff381e7ab7bc7c22203ace1
```

该 Pod 仅运行睡眠进程及只读诊断，不运行 LightRAG 服务，不能报告成应用部署成功。CI 公开日志的部分非敏感字段也被统一 masking，未把带 `********` 的日志记录当作有效产物输入；registry 返回 digest 与 CI 未遮蔽 digest 一致，TKE 实际 imageID 也一致。

## 最新实测阻断

| 项目 | 已验证事实 | 下一步责任与边界 |
| --- | --- | --- |
| 凭据 | 重新读取更新后的 prod 文件；PG `lightrag`、图 `lightrag001` 均可从 TKE 内连接 | 不是要求重贴已有密码；文件未发现独立 runtime/operator 凭据声明 |
| PG 权限 | 只读事务中确认当前用户有 database/public CREATE，无 superuser/CREATEROLE/CREATEDB；`lightrag` 前缀候选角色仅见该账号 | 需运维预置独立最小运行身份；不能据有连接密码推定可自行创建角色，也不能关闭运行权限检查。该查询不声称枚举了所有非此前缀角色 |
| 初始化 | 未见 `lightrag_storage` / `lightrag_coordination` / `lightrag_release` 结构及匹配的业务表；目标图四类 schema 均为 0 | 必须按独立版本化 migration → verify → register → enroll 准备，不靠应用启动补 DDL，不重建 PG/图/S3 |
| 域名 | 当前解析器看到 CNAME `3mji8qpltk7e.eplus2.cn.eo.dnse2.com.`，尚无 A/AAAA 终值；TKE 全局未见该 host 的 Ingress | 已确认正式域名，不再索取域名；需核验并接入实际 EO 域名、云侧证书和 HTTP 回源，不能覆盖其他业务路由 |
| EO 观测 | 最新清单中显式云凭据的 `DescribeZones` 返回成功，但指定根域及无过滤可见列表的 `TotalCount` 均为 0 | 无法据此确认站点、证书、回源或流式参数；不武断认定账号错误或站点不存在，需有权限的现有引用或脱敏运维核验 |
| CLB 观测 | 同一最新输入单次 `DescribeListeners(lb-ao2qcdid)` 返回 `UnauthorizedOperation` | 停止，不换控制器/其他业务身份或扩展应用 COS 权限绕过；需要独立只读入口身份或运维准备 |
| 实现侧入口 profile | 当前发布器仍仅支持 CLB HTTPS/direct，尚不能承接已确认的 EO 入口配置 | **这是交付实现缺口，不是缺 Kubernetes TLS Secret。** 核实真实回源后，应实现显式 EO profile 和对应校验/回归；不填假 CLB certificate ID，不关闭门禁，也不强迫在 K8s 做 TLS |

EO 的站点可见性与域名/源站查询含义依据腾讯云官方 [DescribeZones](https://cloud.tencent.com/document/api/1552/80713)、[DescribeAccelerationDomains](https://cloud.tencent.com/document/api/1552/86336)。本次只执行前者的两次有界只读查询，未声称已经读到加速域名配置。

## 最小运维交接

可直接将以下任务交给持有目标权限的运维，不在聊天粘贴密码：

1. 在现有 prod PG `lightrag` 库、现有 HugeGraph `DEFAULT/lightrag001` 上准备独立 runtime 与 migration/operator 身份。PG runtime 不拥有对象、无数据库/业务或迁移 schema CREATE、无迁移账本及 release 控制写权限；现有账号可作为 operator 候选，先核权限。不新建物理服务、不改原 test。只提供受保护配置/Secret 的引用，实际 migration 暂不代执行。
2. 在实际管理 `eplus2.cn` 的云身份下，核对 `llm-rag.eplus2.cn` 的 EO 站点/域名状态、云侧证书、CNAME、回源目标/协议/Host/端口，以及缓存、缓冲和长流超时策略；回源应按核验结果与 qcloud Ingress 的 HTTP 后端衔接，不导出证书私钥、不创建 K8s TLS Secret。
3. 提供独立入口只读身份的现有引用。至少需要目标 CLB 的 `DescribeListeners` / `DescribeTargets` / `DescribeTargetHealth`，以及目标 EO 站点/域名/七层配置的只读核验能力。不要扩大应用 S3 身份权限；不要提供云控制器或其他业务密钥作为兜底。

运维输入补齐后，先完成 EO 真实 profile 的兼容实现及本地回归，再按批准身份执行首次初始化、部署 Blue **4 Pod × 1 worker**、逐 Pod 与真实入口低负载验收；后续普通发布仍按 4+4，不把首次上线或小样本当作 1000 QPS / SLO / 零停机演练通过。资源/模型额度和未知验收参数仍须按原清单确认，以上不是全部前置已经通过的声明。

## 本次边界

- 目标 kube-system UID `83ba4335-60be-495e-ad3d-9d4fed2567c6`，namespace `prod` UID `bd5be22f-6336-4200-815e-16e79278f213`。
- 仅创建并删除自己的临时诊断 Pod `lightrag-ro-deploy-check-20261023`；UID `99df140b-ac42-4b66-9fbc-50a89fc60a94`，清理后验证不存在。
- 诊断仅 5 次 PG catalog SELECT、1 次图 schema GET；PG 强制只读事务，凭据仅从父进程内存经 stdin 传入，未创建凭据 Secret，未写后端。
- 未部署应用/入口、迁移、recover、重放、发压或切流；未操作原 test 和两个候选；本次未再次 commit/push/tag，未解除静态 gate。
- 脱敏机器证据见[JSON](2026-10-23-prod-deployment-blockers.json)，原始脱敏投影在 ignored `temp/prod-actual-deployment-20261023/`。OpenSpec 仍为 47/59，12 项未完成，未以本次诊断勾选上线或性能任务。
