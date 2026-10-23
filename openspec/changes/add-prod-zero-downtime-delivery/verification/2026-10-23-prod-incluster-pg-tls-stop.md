# prod 内网只读检查：PG 拒绝 TLS 升级后停止（2026-10-23）

用户明确授权延长启动等待、沿用 `prod/lfun`，重建临时诊断 Pod 并按顺序只读核验 PG、图和 S3，结束后删除；异常立即暂停。不部署应用、不切流、不发压、不导入新密钥，不接触原 test、两个候选或 Git 发布。

## 1. 本次临时诊断成功启动

- 固定目标 TKE `cls-px392o6p`，核对 `kube-system` UID 与既有 `prod` namespace UID。复用同 namespace 的 `lfun` imagePullSecret 引用，不读取 Secret payload，不复制/导入密钥。
- 仅创建 `prod/lightrag-ro-diag-20261023-03`，Pod UID `bf744d5b-1a81-447b-97a8-621a34798aa7`。这是诊断对象身份，不是产品版本号或正式 release。
- 使用相同固定镜像 digest `sha256:82be927d0d3e8123179587311f2d12c5f8292fd5ac7fe43cd836d8f65a8614ca`，覆盖应用入口为 Python sleep；不运行服务端、不注入候选源码、不挂载业务卷或 ServiceAccount token，无 Service selector 匹配其独立标签。非 root/只读根、资源限额与有界 `/tmp` 保持。
- 启动观察延长为600秒，Pod activeDeadlineSeconds=900、sleep=840秒，正常终止宽限10秒。北京时间16:26:13 Pulling，16:28:25 Pulled/Created，16:28:26 Started；Running/Ready、restart=0，实际 imageID 与请求 digest 一致。本轮不是启动超时。
- 在不传入后端凭据前验证客户端导入：asyncpg 0.31.0、boto3 1.40.61、httpx 0.28.1。启动与客户端检查通过不等于应用健康或后端验收。

## 2. 真实后端结果及停止边界

随后仅解析 prod 密钥清单，将必要的 PG/图/S3 连接值经 exec 标准输入传给探针进程内存；不出现在 Pod spec、Secret、env、命令参数或远端文件，不打印密码/访问密钥/连接正文。test 清单未读。

| 检查项 | 实际结果 |
| --- | --- |
| PG DNS | 解析1个地址，为 RFC1918 私网地址 |
| PG 连接 | 尝试1次；入口响应拒绝 TLS 升级，ConnectionError，安全分类 `tls_negotiation_rejected_no_plaintext_fallback` |
| PG 已认证会话 | 未建立；账号正确性/权限未验 |
| PG 系统元数据 SELECT | 0条；只读事务及9条预备查询没有执行 |
| 图服务 HTTP/Gremlin | 0次，PG异常后未开始 |
| S3 HEAD/List | 0次，PG异常后未开始 |
| 明文降级/重试/初始化/写探针 | 均未执行 |

本轮得到的是**目标 TKE 到所配置 PG 入口的真实 TLS 协商结果**，不再以本机超时代表内网不可达。对端能回应拒绝升级，不等于 PG 宕机，也不能证明已连到通过认证的目标数据库；未读取服务端或代理配置，不能确定是服务端未启 TLS 还是接入层不支持。

探针采用明确要求 TLS、不自动明文回退的策略。本次停止是该策略与入口现状不匹配；**是否将 TLS 作为上线硬要求，仍取决于待确认的生产安全策略，不由探针自行设定业务前置条件**。不自动修改 PG 配置，不开放公网，不换端点或关闭客户端 TLS 重试。后续应确认既有 PG 的预期传输策略或现成 TLS 入口；若生产明确采用受控内网非 TLS，应先确认允许探针按该策略连接。图/S3可以另行明确授权独立核验，不借本轮“异常即停”继续请求。

探针在派发前还对照当前 HugeGraphClient 纠正了预备 Gremlin 路径：使用实际 REST `/gremlin` 和图别名，不将清单中额外8182端点误当当前应用必需路径；仅常量表达式、无图业务正文。此准备从未发往服务端，不是图验证结果。

## 3. 清理与非干扰

- 首异常返回后立即停止新增后端请求，随后只做正常删除和只读复核。16:28:43 Killing，16:28:55删除完成；16:29:23再次确认该 Pod 不存在。创建=1、删除=1，无残留诊断资源；未 force 删除或创建第二个 Pod。
- 原 namespace 的27个既有 Pod、3个 Deployment/StatefulSet，UID及spec hash与本轮创建前一致。原 test 和两个候选未访问；未部署应用、切流、发压、导入新密钥、变更 PG/图/S3 或 Git 发布。
- 原始脱敏投影、无凭据脚本/manifest位于仓库私有 `temp/prod-diag-20261023-03/`；正式证据见[脱敏 JSON](2026-10-23-prod-incluster-pg-tls-stop.json)。无业务正文、SQL错误正文或Secret正文。

## 4. 验收状态

**本次未完成全部三项内网校验。** 仅诊断环境启动、客户端导入、PG私网解析/拒绝TLS响应和资源清理取得真实证据。PG认证/权限/schema/容量、图/S3内网访问，以及功能、4+4、SLO或生产放行均不签署。

本轮仅临时运维脚本与证据文档，不修改应用、测试、原流水线、部署模板或正式未知参数。主任务仍41/53完成、12待办、57场景；文档验证不冒充后端通过，未重跑应用单测。
