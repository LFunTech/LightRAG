# 首次生产发布准备：本地验证通过，生产放行未完成

日期：2026-10-23。源码基线：`master` / `beb4d64681c06d393255c577cdf0d988fdeecbfb`。
本记录为 Git 写入前的准备快照，不是实际部署、tag 或流水线成功回执。

## 授权与现场边界

- 用户要求优先尽快部署稳定版到 prod，已明确允许本次生产必需改动的 commit、push 和正式生产 tag。
- test 只读预检因优先级改变而暂停：原五套10 Pod和两个候选身份/spec/配置引用前后一致；诊断 Pod创建0、后端请求0、部署/迁移/发压0。资源快照11节点Ready，168个容器缺CPU或内存requests，5个Pending Pod；未继续查因，不据requests快照断言可调度性或服务健康。
- prod 仅重新核对目标TKE身份及指定 namespace 工作负载；三个既有Deployment未发现LightRAG，不据此断言其他namespace/外部writer为空。没有生产写入、导入密钥、应用部署或切流。
- 既有业务计量模块是当前查询/OpenAI代码的导入依赖，纳入此次必要代码交付，模块SHA256与原实施前记录相同；不代为实现或启用外部authority，不认定计费/审计可靠性已通过。
- 排除 `.playwright-cli/`、私有配置、密钥、kubeconfig、临时结果和构建产物，不提交其他仓库内容，不改原test流水线或现场。

## 实际本地验证

| 检查 | 结果及限制 |
| --- | --- |
| 全后端首次运行 | 6 failed / 9655 passed / 384 skipped / 2 warnings；失败日志保留，不用于放行 |
| 失败定位与局部复验 | Cairo已安装但系统Bash启动链清除DYLD路径；dotenv测试子进程误继承父进程的私有配置隔离标志，仅修测试夹具排除项。直接Python运行223 passed，未改业务超时、依赖或机器配置 |
| 全后端最终里程碑 | **9661 passed / 384 skipped / 2 warnings / 0 failed，194.35s**；未启用真实integration或负载 |
| Python静态检查 | 143个发布相关Python文件Ruff通过；另1个既有计量测试保留10项E701，不声称全量Ruff通过 |
| 干净源码打包 | 从明确路径导出1655文件；setuptools PEP517后端构建wheel/sdist，二者均匹配65项必需源码/迁移资源，无密钥/temp/kubeconfig/node_modules路径 |
| 产物用途 | 本地资源与依赖完整性验证，不是已构建/推送的正式容器镜像或生产digest |

最终测试命令：

```bash
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib PYTHON_DOTENV_DISABLED=1 \
  .venv/bin/python -m pytest tests
```

直接运行Python是本机Cairo加载所需；父进程继续禁止读本仓库私有`.env`，测试子进程只读取自己临时cwd的合成fixture。两条弃用warning来自既有websockets/Uvicorn路径。打包时`python -m build`因环境没有对应CLI而不可用，改用项目声明的`setuptools.build_meta`；工作站原目录打包含本机依赖目录，已废弃，未推送该产物或更改打包策略来掩盖问题。

静态门禁、原test基线及打包hash见[机器记录](2026-10-23-first-release-preparation.json)。原始完整日志保存在本仓库ignored目录`temp/prod-first-release-20261023-1830/`，失败与成功日志分别保留，不累加历史测试数。

## 生产阻断及推进顺序

1. **确认目标输入与身份**：独立runtime/operator配置引用、生产域名及TLS/Ingress引用仍待用户答复；只提供引用，不提供密码。既有PG账号有CREATE，不直接作为最小runtime；没有CREATEROLE也不能靠工具凭空创建受限身份。
2. **补真实首次接入前置**：固定正式镜像后，按批准数据域执行独立`plan → apply → verify → register → enroll`；PG/图身份、S3 GET/写删/ACL、工作路径与持久化/恢复语义、资源/模型/后端预算均需核验。历史内网只读通过不代表这些写权限/初始化已经完成，单次非TLS探针授权也不自动成为应用长期传输策略。
3. **明确首次验收与开放范围**：空目标首发基线仍4 Pod×1 worker；先逐Pod协调/polling、单文档S3入库/查询/流式及真实入口验证，再按批准边界开放。首次上线与4+4升级验证不是同一件事；CLB长流、8Pod发布及1000QPS/质量/SLO未通过。用户要求尽快上线不等于默许降低性能/正确性验收或编造onboarding证据。
4. **最后才正式tag放行**：在生产前置和验收批准齐备后解除静态gate、选用未占用的正式semver并推送。当前只允许先交付必要代码，不提前打tag、不误用历史test镜像、不以关闭gate后“只构建成功”报告生产部署完成。

完整OpenSpec仍 **47/59完成、12项未完成**；9.2–9.4未勾，不自动归档。此报告不能作为1000QPS、4+4或已上线的证据。
