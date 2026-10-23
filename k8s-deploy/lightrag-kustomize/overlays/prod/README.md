# Production overlay

此目录只提交生产接入 schema 与说明，不包含可误应用的真实目标、Secret 或测试默认值。

配置经过 `lightrag_delivery.config.load_prod_config` 校验后，由
`lightrag_delivery.render` 生成独立的 slot 资源。离线渲染入口：

```sh
python -m lightrag_delivery --config <approved-config.json> \
  --identity <verified-identity.json> --environment <private-runtime.json> \
  --image <verified-registry-repository> --slot green --output <new-private-directory>
```

输出是权限为 `0700/0600` 的新 Kustomize overlay，拒绝覆盖现有目录；可能含 Secret，
不得提交、上传或将 `kubectl kustomize` 的真实输出写进公共日志。初始接入资源仅在显式
`--include-onboarding` 时渲染；该选项不执行初始化、迁移或 apply。

正常流水线不重写稳定入口或 Blue 配置。固定镜像内的受限 control Job 先核验已有接入，
只准备 inactive slot，再执行可观测的切流及交权。原 `base/`、`overlays/test/` 不复用为
生产默认配置，也不修改。

生产使用腾讯云 `qcloud` Ingress：当前适配范围为 HTTPS 七层入口、CLB 直连 Pod、
原 HTTP 后端、已核验的流式超时/连接保持合同。NodePort、TCP 或未完成演练的入口拒绝启用。
详细步骤、权限和上线门禁见 [生产发布手册](../../../../docs/ProdDelivery.md)。
