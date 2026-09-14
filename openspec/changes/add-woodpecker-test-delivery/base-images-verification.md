# 本机基础镜像同步验证（2026-09-14）

本记录只覆盖用户追加授权的基础镜像同步与 Dockerfile 替换，不表示整个 Woodpecker
proposal 已实施、远端应用镜像已构建或 Kubernetes 测试部署已完成。

## 执行与内容一致性

- 执行位置：本机 macOS arm64，原生 `regctl 0.11.6`；读取已有 Docker credential helper。
- 六个原始 tag 先解析为固定 index digest，再从该 digest 同步到
  `docker-hub.f123.pub/base/` 中的独立 LightRAG 标签。
- 使用 `regctl image copy --force-recursive`，未指定单平台过滤；没有导入全部镜像到
  Docker Desktop VM，没有使用 Kubernetes 或 Woodpecker 进行同步。
- 六个目标 index 的 SHA-256 均与源一致；目标端递归校验 **54 个 manifest/index** 的
  摘要，并成功检查 **167 个去重后的 repository/blob 引用**（config/layer）的可读性。
  此计数含源 index 中的 attestation manifest，不包括独立签名/referrer 的额外复制。
- 内容验证完成时间：`2026-09-14T01:33:20.167896+00:00`。Dockerfile 引用替换发生在
  六个镜像同步和上述检查全部完成之后；未覆盖其他项目公共标签，未删除任何镜像或卷。
- 完整来源、目标标签、digest、平台见
  [镜像锁定清单](../../../scripts/ci/base-images.lock.json)。

## 实际验证结果

| 验证项 | 结果 |
| --- | --- |
| 基础镜像来源回归测试，修改前 | 4 failed，正确拒绝原有公网引用和缺失镜像清单 |
| 同一测试，修改后 | 4 passed |
| `./scripts/test.sh tests/setup` | 369 passed，30.08 秒 |
| 三个 Dockerfile × amd64/arm64 的 `buildx --check --pull` | 6/6 通过，均无 warning |
| registry 匿名读取 | 拒绝：unauthorized；现有本机凭据读取成功 |

本机 Docker driver 拒绝单条请求中的多平台参数列表，首次组合检查在读取 Dockerfile
前报 `Multi-platform build is not supported for the docker driver.`。后续逐个平台
执行相同检查全部通过，没有修改 Docker Desktop 存储设置、创建新 builder 或提权。

验证命令使用 `PYTHON=/tmp/lightrag-distributed-test-python` 运行 setup 测试。
pytest 的全局会话提示本地没有安装 spaCy 模型；本次 setup 子集没有测试被跳过。

本机内容与构建检查原始记录目录：
`/var/folders/p0/fjxtswnn6cq2m9x_15rvdkj40000gn/T/lightrag-base-mirror-20260914-_ce4mn5v/`。
setup 测试输出：`/tmp/lightrag-base-images-setup-tests.log`。这些临时路径仅用于本机追溯，
可复现的来源清单和操作步骤已提交到仓库，不依赖临时文件才能使用镜像。

## 未执行项与接入前置

- 未重新完整构建应用/数据库镜像；本次 `--check` 仅验证 Dockerfile、镜像引用及元数据，
  不执行所有 RUN。基础镜像内容保持原源 digest，应用构建内容未作其他修改。
- 未触发 Woodpecker、创建 release/tag、修改 test 集群资源或重启本机服务。
- 既有 GitHub Actions 和后续 Woodpecker 需要内部 `base` 仓库读取身份；仅登录 GHCR
  不够，也不能给 PR 提供镜像推送权限。registry 仍禁止匿名读取，没有修改访问策略。
- 其余 proposal 实施任务仍待批准；本记录不解决先前全量后端测试中的两个鉴权状态码失败。

后续更新与本机复核步骤见
[容器基础镜像说明](../../../docs/ContainerBaseImages.md)。


## Kaniko 切换后的输入收敛（2026-09-14）

后续 Woodpecker 构建改为 Kaniko，`Dockerfile` 与 `Dockerfile.lite` 删除了不再使用的
BuildKit parser frontend directive。因此当前 `scripts/ci/base-images.lock.json` 只记录实际
Dockerfile 外部 FROM/COPY 输入（五类来源），不再包含 `docker.io/docker/dockerfile:1`。
上方“六个原始 tag”的同步记录是当时完成基础镜像迁移的历史证据，不再表示当前
Dockerfile 输入数量。
