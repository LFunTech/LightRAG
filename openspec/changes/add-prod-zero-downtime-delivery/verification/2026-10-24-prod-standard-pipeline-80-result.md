# 正式生产配置发布 v1.5.11 / #80：成功

- 流水线：[正式tag #80](https://woodpecker.f123.pub/repos/57/pipeline/80)，commit `05e8e8efbdf142685816d22bed95b51e6f869ef9`，北京时间完成 `2026-10-24T10:16:27+08:00`。
- 原有validate-release→build-image→pre-deploy→deploy-prod全链路实际8步骤全部success；不是manual续部署。#79已按用户要求取消，未进入prod部署。
- 生产部署步骤仅Kustomize apply/rollout。`prod/lightrag-blue`实际4/4 Ready、4 available；流水线输出4Pod Running且重启0。
- Ingress实际qcloud、`lb-ao2qcdid`、`llm-rag.eplus2.cn`，控制器Ready=True/Success；K8s无TLS。
- 本次是配置发布：线上应用继续复用v1.5.9原digest `sha256:496b46a29266d62895842d09d7ce132be9c4a326699e32095467bebc13b8d4db`。正常构建阶段确实产生v1.5.11镜像，但生产profile未选择它，不能将该新镜像伪报为已上线；应用代码、runtime/config/policy未调整，也没有重跑初始化/migration/enroll。
- 原test工作流、实例、数据和两候选不变。只完成流水线/Pod部署结果确认，没有追加后端、云API、模型或压测；此前额外5分钟观察已中止，不能算验收完成。
- 源码阻塞修复：正式prod调用已有GitHub API来源证明，避免unshallow全历史；test/pre仍原git默认路径。#80实际已完成归档并通过全部后续步骤。
- 首次声明式apply的last-applied annotation警告由kubectl自动补齐，仅资源管理注解，不是部署失败或应用改动。

详细机器可读结果见[实际结果JSON](2026-10-24-prod-standard-pipeline-80-result.json)。原12项容量、零停机/4+4、外部入口等未验任务不因本次K8s发布成功而完成。
