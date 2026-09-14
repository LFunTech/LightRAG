# 本机跨节点 Pod 并发验收

这是显式执行的破坏性**隔离测试**，不会被普通 pytest 自动部署。
使用实际仓库 Dockerfile、Helm chart、PostgreSQL/pgvector 和 HugeGraph；仅模型服务使用确定性 fixture，避免计费和抽取随机性。`gateway.py` 不模拟图存储，原样转发实际 HugeGraph 请求并记录耗时。

## 范围与限制

- 测试配置每 Pod `HUGEGRAPH_MAX_CONNECTIONS=1`，两个 Pod 合计仍可同时发出两路真实 HTTP 写请求；不能超过小型 HugeGraph 实例的 batch 写并发预算。默认连接数不构成集群级限流。
- 两个应用 Pod 必须位于两个不同的 Kind worker 节点；验证实际镜像 ID、源码摘要和跨节点共享文件可见性/独占创建。
- 同 workspace 不同实体的真实图 HTTP 写区间必须重叠；计时不含人为等待的同步屏障，不是吞吐基准。
- 共享实体/关系来源合并、权重、tracking、文档锚点和真实向量记录；重复提交只抽取一次。异步检测产生的 `dup-*` / `metadata.is_duplicate` 审计记录应保持 FAILED 并指向主文档，不应误计为主文档处理失败。
- 两个活跃 document claim 时 clear 返回 typed 409；跨 Pod pause、显式 resume、FAILED 不自动重试。人工重试的 HTTP 接受不代表已完成；脚本仅允许等待原 FAILED 版本，新失败版本仍立即拒绝。
- 空闲应用 Pod 正常替换后保留全部文档。这里**不证明**强杀、节点故障、网络分区或自动接管；已有独立进程故障矩阵见 `../test_process_acceptance.py`。
- Kind 的两个节点共享同一物理主机上的挂载目录，不是生产 NFS/云 RWX 验收。HugeGraph fixture 不挂持久卷，不测试 HugeGraph Pod 重建。
- 所有 Service 均为 ClusterIP。验收连接只绑定 `127.0.0.1`，不部署 Ingress，不连接已有业务服务或百炼。

## 准备并启动

从仓库根目录执行。依赖 Docker、Kind、kubectl、Helm、Python 3.12+ 和 PyYAML。Docker VM 需要足够磁盘容纳完整构建与两个节点的镜像副本（建议开始前至少 30 GB 可用）；宿主机空闲不等于 Docker VM 空闲。不要未经授权清理其他项目资源。

必须使用全新的测试目录/集群/数据库，不能对有业务数据的集群套用此流程。脚本拒绝覆盖非空目录，也拒绝将凭据写入仓库。固定集群名 `lightrag-concurrency`；如已存在，应先检查，不能盲目删除。

```bash
RUN=/tmp/lightrag-k8s-acceptance-$(date +%Y%m%d-%H%M%S)
TAG=$(git rev-parse --short=9 HEAD)-k8s-verify
python tests/distributed/kubernetes/prepare.py \
  --directory "$RUN" --image-tag "$TAG" \
  --workspace "local_debug_k8s_$(date +%Y%m%d_%H%M%S)"
kind create cluster --name lightrag-concurrency \
  --config "$RUN/kind.yaml" --kubeconfig "$RUN/kubeconfig" --wait 120s

# Explicit target on every command; never change global kubeconfig/context.
K=(kubectl --kubeconfig "$RUN/kubeconfig" --context kind-lightrag-concurrency)
NS=lightrag-concurrency-test
"${K[@]}" create namespace "$NS"
"${K[@]}" -n "$NS" create secret generic lightrag-test-credentials \
  --from-env-file="$RUN/credentials.env"
"${K[@]}" apply -f "$RUN/infra.yaml"
for name in postgres hugegraph gateway; do
  "${K[@]}" -n "$NS" rollout status "deployment/$name" --timeout=300s
done

docker build -t "lightrag:$TAG" .
kind load docker-image "lightrag:$TAG" --name lightrag-concurrency \
  --nodes lightrag-concurrency-worker,lightrag-concurrency-worker2
```

如果节点无法拉取基础设施镜像，可先 `docker pull` 对应镜像，再用 `kind load docker-image` 导入实际调度节点。不要为省事删除无关镜像、容器或数据卷。

## 显式 bootstrap，再启动两个 writer

```bash
H=(helm --kubeconfig "$RUN/kubeconfig" --kube-context kind-lightrag-concurrency)
"${H[@]}" install lightrag k8s-deploy/lightrag -n "$NS" \
  -f k8s-deploy/lightrag/values-distributed.yaml -f "$RUN/values.yaml"
"${K[@]}" -n "$NS" wait --for=condition=complete job/lightrag-bootstrap --timeout=300s
"${K[@]}" -n "$NS" logs job/lightrag-bootstrap

# Keep zero writers until bootstrap has completed. Test-only anti-affinity
# proves cross-node execution without changing the production chart.
"${H[@]}" upgrade lightrag k8s-deploy/lightrag -n "$NS" \
  -f k8s-deploy/lightrag/values-distributed.yaml -f "$RUN/values.yaml" \
  --set maintenance.enabled=false
"${K[@]}" -n "$NS" patch deployment lightrag \
  --patch-file "$RUN/writers-patch.yaml"
"${K[@]}" -n "$NS" scale deployment lightrag --replicas=2
"${K[@]}" -n "$NS" rollout status deployment/lightrag --timeout=300s
"${K[@]}" -n "$NS" get pods -o wide

python tests/distributed/kubernetes/verify.py \
  --kubeconfig "$RUN/kubeconfig" --credentials "$RUN/credentials.env" \
  --output "$RUN/acceptance.json"
```

成功才会输出最终 JSON；失败应检查两侧应用日志、gateway 事件、协调器状态，不能将失败当作通过或直接重复对已写入的 workspace 运行。脚本会释放模型屏障并关闭端口转发，但不会删数据、自动 recover 或销毁集群。故障后的 fence 必须依照 `docs/DistributedDeployment.md` 审计处理。

凭据、kubeconfig、生成的 Secret 和原始运行日志留在仓库外。共享目录仅为一次性本机测试设置宽松权限；生产部署应使用受限的 RWX 权限和 Secret 管理。测试结束后按需人工清理**该专用**集群；脚本不会执行全局 Docker prune。
