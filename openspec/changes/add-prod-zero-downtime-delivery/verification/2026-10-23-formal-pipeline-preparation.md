# 最高部署规则与正式流水线准备快照

日期：2026-10-23；提交前源码HEAD为`1664db64b00fe6910fbdfa9ae4e51515db819a16`。
本记录在Git写入/触发前生成，不是pipeline成功、正式镜像或生产部署回执。

## 用户授权与最高规则

- 用户要求将“禁止在K8s做应用TLS”写入最高规则，再跑流水线；已有本次生产必要commit/push/正式tag授权继续适用。
- 规则已置于本仓库根`AGENTS.md`最前部署节，覆盖仓库部署文档/示例/默认值，但不越过系统/开发者/当前用户指令；不修改其他仓库或用户全局规则文件。
- TLS终止和证书管理在云侧EdgeOne/CLB，Service/Pod只HTTP。禁止本应用的TLS Secret、证书/私钥注入或挂载，以及Pod/Ingress控制器/sidecar内TLS终止。客户端出站HTTPS、Kubernetes控制面和其他既有业务TLS不变。
- 本轮必要代码已移除`tls_secret`输入，拒绝SSL/证书路径并显式Pod `SSL=false`，原test路径不变；外部云证书/HTTPS/direct/流式准入不放宽。

## 流水线执行边界

远端最新正式tag为`v1.5.7`，本次候选正式tag为 **`v1.5.8`**；创建前仍须确认未被占用、必要源码先commit/push到master，不覆盖tag。既有 `validate-release → build-image → pre-deploy` 仍复用原产物链。

**`.woodpecker/deploy-prod.yml` 的静态gate继续false**，prod未初始化/接入，账号、域名及云入口只读权限等阻断见[引用核对](2026-10-23-prod-reference-audit.md)。因此本次只运行正式产物构建/校验，不触发test部署，也不导入prod密钥、迁移或切流。生产阶段跳过不能报告成生产部署成功。

Woodpecker只读public lookup确认仓库`LFunTech/LightRAG`（repo id57）已激活，server3.19.0；没有读取CI/admin token或其他业务凭据，不变更CI服务/配置。实际tag和pipeline结果必须以推送/API回执为准，不能仅凭本准备记录宣称已触发。

## fresh本地验证

```bash
PYTHON_DOTENV_DISABLED=1 PYTHON="$PWD/.venv/bin/python" \
  ./scripts/test.sh tests/ci tests/distributed/release tests/provisioning tests/api/config
```

结果：**405 passed / 60 skipped / 2既有弃用warning，2.92s**。只覆盖本轮相关目录；真实integration默认未启用，原9661属于先前提交的全套快照，不用于证明本轮新增代码全套通过。

静态、敏感路径、原test交付基线及实际发布范围见[机器记录](2026-10-23-formal-pipeline-preparation.json)。原test/两个候选及prod应用本轮未访问/修改，未重启/recover/重放或发压。OpenSpec47/59、原12项真实未完成与63场景不变；不勾9.2–9.4，不归档。
