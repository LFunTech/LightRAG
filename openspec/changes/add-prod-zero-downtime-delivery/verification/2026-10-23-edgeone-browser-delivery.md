# EdgeOne 与阿里云 DNS 实际配置记录

日期：2026-10-23。用户逐项批准仅配置 `llm-rag.eplus2.cn` 的域名、云侧证书、缓存旁路、流式参数及阿里云专属 CNAME；不改变全站、`agent-base.eplus2.cn`、原 test 或两个候选，不启用付费增值功能，不在 K8s 做 TLS。

## 已完成的真实云配置

| 项目 | 实际核验结果 |
| --- | --- |
| EO 站点 | `eplus2.cn` / `zone-3mezyvnyozb0`；复用已有站点及基础版套餐 |
| 新域名 | `llm-rag.eplus2.cn`，最终控制台显示 **已生效**；参考域名仍显示已生效，未修改它 |
| 回源 | 已保存后重新进入编辑页只读核对：IP `1.15.197.243`、HTTP、80、Host `llm-rag.eplus2.cn`；随后取消退出，无二次配置保存 |
| 云侧证书 | 复用 EO 已有可用免费证书，最终 **已部署**；SHA256-RSA，到期北京时间 `2027-01-02 15:26:07`，到期前15天自动更新；未导出证书或私钥 |
| 域名专属规则 | `llm-rag-API-no-cache-stream`；仅 HOST 等于 `llm-rag.eplus2.cn`；规则列表从20增至21条，新规则启用 |
| 缓存/流式参数 | 节点和浏览器不缓存、离线缓存关闭、Brotli/Gzip关闭、HTTP应答无数据超时120秒；用户对这五项另行明确批准后才保存并发布 |
| 阿里云 DNS | 精确搜索 `llm-rag` 原有0条专属记录；仅新增1条默认线路 CNAME → `llm-rag.eplus2.cn.eo.dnse2.com`，TTL600秒，启用，单目标权重1；未改通配或其他记录，未启用健康检查付费项 |
| DNS 验证 | 权威 `dns25.hichina.com` 与本机递归查询均返回新 CNAME；分配的 EO 名称有 A 终值；EO 短暂旧缓存提示后刷新成为已生效 |

全部云变更均通过用户现有浏览器登录态完成。阿里云账号页及新版 DNS origin 曾被安全审核拦截，先由用户登录，再对准确 origin/记录取得明确批准后操作；未换工具、凭据或请求通道绕过。截图保存在 ignored 的 `temp/prod-edgeone-browser-20261023/`，未上传或提交；脱敏字段及截图 SHA256 见[JSON](2026-10-23-edgeone-browser-delivery.json)。

120秒是节点等待源站数据的空闲超时，不是 RAG 响应SLO或完整流的最大时长。设置含义参见腾讯云官方[回源超时时间](https://cloud.tencent.com/document/product/1552/104992)；压缩行为参见[智能压缩](https://cloud.tencent.cn/document/product/1552/70748)。本次保守关闭该域名边缘压缩，不把配置保存等同真实流式首包/连续性验证；未启用 WebSocket 或付费智能加速来冒充 SSE 支持。

## 同步的本地交付实现

- 新增显式 EO observer/profile，保留 direct/CLB HTTPS路径。EO绑定准确域名、单源站、云侧HTTPS、经审阅的有序规则/全局配置hash和独立观测身份；真实Transport核对实际Ingress VIP、HTTP80监听器、全部CLB后端及公开候选身份，不接受K8s TLS漂移。
- prod清单补已有 `S3_ENDPOINT_URL` / `S3_BUCKET` / `S3_FORCE_PATH_STYLE` 标签；不改test映射或跨环境兜底。
- 生产slot使用相同固定digest的无Secret、non-root initContainer准备工作卷子目录，避免LOG_DIR缺失；源文档仍用S3，工作卷并非源文档持久存储替代。
- 新鲜审查为0 Critical / 0 Important / 1 Minor；唯一Minor为JSON列表/对象触发非预期TypeError。补回归先见4 failed/6 passed，再类型校验后10 passed；未放宽协议、角色或默认拒绝。
- 定向本地命令 `PYTHON_DOTENV_DISABLED=1 PYTHON="$PWD/.venv/bin/python" ./scripts/test.sh tests/ci tests/distributed/release tests/provisioning tests/api/config`：**460 passed / 60 skipped / 2既有warning / 0 failed**。非远端integration或容量测试。
- scoped Ruff、8文件format、5份Woodpecker配置strict lint、prod脚本shell语法及OpenSpec strict通过。原构建/验证workflow、test workflow/脚本/overlay与`b79ba11`的diff为空；没有触发test发布。
- 从已跟踪源码及明确的新EO模块导出干净构建目录，isolated `uv build` 使用本仓库temp内cache生成wheel/sdist；6项变更源码在两种包内均逐字节匹配，受禁目录路径0。该本地包不是新的生产镜像。初始`python -m build`缺少可执行模块、默认uv cache不可写，改用隔离构建/仓库内cache后成功；初版包检查误把uv生成的`.gitignore`当tar，收紧为准确产物glob后两包验证通过，未改业务代码绕过。

## 不能据此签署的上线前置

**应用仍未部署，数据库尚未迁移，未执行RAG/SSE端到端测试或1000QPS压测。** 浏览器可配置入口不代表流水线已经具备自动化入口核验身份，也不能用截图填充EO API返回对象的审阅hash。

1. `.secrets/prod.secrets`仍为同一已核验PG/图登录，没有预置的独立runtime/operator声明。PG初始化表结构不需要CREATEROLE；应用也不应取得CREATEROLE。实际阻断是批准合同要求独立最小运行权限，现有账号有CREATE且不能自行创建角色，需运维预置运行身份并提供受保护引用，不能清除权限门禁。
2. 已有云API身份对目标CLB无只读权限、EO可见站点为0；浏览器主账号登录态未导出为CI凭据。需现有合格EO/CLB只读引用，或单独批准的最小IAM准备，不借用应用S3、控制器或其他业务身份。
3. 现有日常`prod_deploy`消费已登记的发布基线；首次空环境还需实际迁移→verify→register/enroll、首个4×1 slot/入口准备与验收。首发流水线串接仍属实现/交付待办，不是要求用户提供一个虚假的已有Blue；不得预置不存在的健康实例以绕过普通发布器。
4. S3实际对象写/读/删与ACL、工作卷、资源/模型额度、发布预算及真实连接保持/质量证据仍未齐备；入口配置完成不补齐这些空项，也不宣称4 Pod已满足1000QPS。

额外一次标准系统DNS/CA/hostname校验的公网TLS连接，在系统解析阶段返回`gaierror`，未建立TLS会话；未关闭证书校验、修改本机DNS或绕过解析重试。权威/递归dig正确与本机系统解析器不同步需区分，此结果不证明云证书错误，也不签署公网TLS握手或RAG端到端通过。

本次只读刷新prod namespace的Deployment/Job/Ingress/ServiceAccount，仍仅原三个业务Deployment/Ingress及default SA，没有LightRAG应用、Job或受限发布SA。本轮未创建/修改K8s资源、导入密钥或修改PG/图/S3；原test及两个候选未访问。prod静态gate保持关闭，不再为“推进”重复打只构建不部署的正式tag。原**47/59、12项真实待办**不变，新增EO本地场景不被计为真实上线或性能任务完成。
