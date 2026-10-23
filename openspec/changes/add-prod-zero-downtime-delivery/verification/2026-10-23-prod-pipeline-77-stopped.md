# #77 应用续部署实际失败与停止记录

## 授权及实际执行

用户要求尽快完成prod，但本次范围仍为：通过流水线复用#76已成功初始化结果、v1.5.9原镜像digest和配置，仅续部署应用；不重做migration/enroll、不重建镜像/覆盖tag，异常立即停止。原test和两个候选不动，K8s不做应用TLS，不新增CLB/EdgeOne门禁。

- 交付工具commit：`4e7b16205f1835b41098b2ba13cc939bda45df74`（master）。应用仍为v1.5.9/`18df4f5c389d8f5e5fbd258f5f42316a4ec06421`。
- 原镜像digest：`sha256:496b46a29266d62895842d09d7ce132be9c4a326699e32095467bebc13b8d4db`，没有构建新镜像或新建/覆盖tag。
- 只触发一次manual/master流水线#77，仅选择`resume-prod-app > resume-prod-app (#2)`。步骤北京时间2026-10-23 23:32:12开始，23:35:51失败停止。未调用整条#76重跑。
- 日志已证明`RECEIPT_VERIFIED`及`CREATE_MISSING_APPLICATION_RESOURCES`；创建了应用ConfigMap、`prod/lightrag-blue` Deployment及4个Pod。
- 既有prod kubeconfig仅扩展manual/tag事件，值未变；其他CI Secret元数据不变，没有新密钥导入。

## 实际状态，不是上线成功

北京时间23:50再次只读复核：4个Pod全部非Ready，容器自动重启数分别7/6/6/6，Deployment期望4但无ready/available副本。明确按名称核对`prod/lightrag` Service和Ingress均不存在；不能用不匹配的label查空结果替代按名称确认。没有创建本次应用入口，也未执行真实业务查询/入库/发压。

正常启动流程可能已写入协调/实例元数据，**不能声称后端完全没有写入**。没有显式migration/enroll、recover、业务重放或数据库修复；没有手动重启、删除、替换或缩容Pod。Kubernetes的`restartPolicy=Always`仍会自行重启失败容器，并不因流水线停止而停止。不得未经授权缩容为零，避免删除emptyDir首异常日志和现场。

## 已区分的三类问题

### 1. 已证实的交付器兼容缺陷，仅本地修复

对实际Deployment/Pod快照与原渲染结果逐路径比较，额外差异为：

- Downward API `env[*].valueFrom.fieldRef.apiVersion=v1`；
- 仅绑定后的TKE Pod注入`resources.requests/limits["tke.cloud.tencent.com/eni-ip"]="1"`。

严格行为比对未归一化这些已观察到的默认/平台字段，导致#77拒绝继续。修复只接受上述`v1`及绑定Pod中requests和limits**同时精确为1**的ENI项；Deployment模板里的ENI、自定义数量或其他运行行为仍拒绝。实际快照在本地修复后的Deployment及Pod比较均通过；这不是生产复测。

另补字段级`ProdConfigError`的安全原因输出；其他异常仍仅显示类型，不输出原始stderr/凭据。两项回归先2 RED，修复后GREEN；相关目录最终719 passed /146 skipped /2既有warnings。日志在忽略目录`temp/prod-app-resume-20261023/`，不将私有快照或输入提交。

### 2. 应用真正启动失败，根因尚未确认

实际错误链为`lifespan → initialize_storages → runtime.initialize → release.initialize → store.register → ReleaseConflict: Release runtime identity or state conflict`，应用随后退出。部分Pod还有TKE网络分配等待，例如一个Pod约6分4秒后才取得IP；不能据此认定是镜像拉取错误，也不能用延长流水线等待解决注册冲突。

源码将register SQLSTATE `P0001`统一映射为该异常。注册函数既可能拒绝scope/profile/config不一致，也可能拒绝同instance已有不同coordinator owner或closed记录；后续自动重启产生的新owner还可能掩盖首次失败。**尚无只读数据库证据，不能认定具体哪一分支，更不能清记录或重新enroll碰运气。** CRI的`--previous`日志不保证保留首次启动；Pod的日志scratch仍可能有首异常，需保持原Pod。

### 3. 开放入口前的认证配置风险，未擅改原配置

应用明确警告默认`WHITELIST_PATHS=/health,/api/*`会令`/api/chat`、`/api/generate`等Ollama兼容路由跳过已启用的认证。原批准runtime未显式收紧该字段；本次未创建Service/Ingress，但不能按该默认配置直接认领安全可用。

不静默添加env绕过配置hash，也不把修改运行配置伪装成“原配置”。需要先诊断注册冲突，再审阅认证配置以及对原identity/hash/登记的影响。认证收紧是应用交付风险，不是追加CLB/EdgeOne门禁。

## 下一步最小范围

已请求允许：仅从现有新故障Pod读取工作盘首异常日志，并用只读事务查询该workspace的release登记元数据；不新建Pod、不重启、不migration/enroll、不改数据库/配置，不输出凭据。**答复前不执行Pod exec或SQL。**

取得证据后给出具体修复及最小变更范围；如需改变原镜像、配置、登记或实例身份，必须与本次“原产物/原配置、仅应用续部署”的边界明确区分，不直接再次触发流水线。未知性能/模型额度参数仍未知，不发1000QPS压测。

## OpenSpec状态

仍为60/74，14项剩余，73场景；13.6/14.3实际部署均未完成。本地交付器兼容修复通过不代表应用可启动、业务完整可用、外部入口正常或4+4/性能达标。不访问原test或两个候选，不替历史验收勾选任务。
