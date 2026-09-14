# 容器基础镜像的本机同步

本 fork 的 `Dockerfile`、`Dockerfile.lite`、`Dockerfile.postgres` 只从
`docker-hub.f123.pub/base/` 读取外部镜像，包括 `COPY --from` 引用的 uv
二进制镜像。内部构建阶段的别名不属于外部镜像。Woodpecker 发布构建使用
Kaniko，因此 Dockerfile 不保留 BuildKit parser frontend directive。

完整来源、目标标签、index digest 和平台清单见
[`scripts/ci/base-images.lock.json`](../scripts/ci/base-images.lock.json)。
Dockerfile 使用 `内部仓库:独立标签@sha256:完整摘要`，不能只固定可变 tag。

## 镜像对应关系

| 用途 | 原始来源 | 内部仓库 |
| --- | --- | --- |
| 前端构建 | `docker.io/oven/bun:1` | `docker-hub.f123.pub/base/bun` |
| Python 运行环境 | `docker.io/library/python:3.12-slim-bookworm` | `docker-hub.f123.pub/base/python` |
| uv 二进制 | `ghcr.io/astral-sh/uv:latest` | `docker-hub.f123.pub/base/uv` |
| PostgreSQL/pgvector | `docker.io/pgvector/pgvector:pg18-trixie` | `docker-hub.f123.pub/base/pgvector` |

独立标签格式为 `<原始标签>-lightrag-<源 index digest 前 12 位>`。
同步不得覆盖其他项目使用的 `base/python:...` 等共享标签。若独立标签已经存在，
先比较 digest；内容冲突必须停止，而不是覆盖。Dockerfile 中的完整 digest
是最终内容约束，短摘要只用于让标签可识别。

## 凭据与网络边界

内部 registry 拒绝匿名读取。开发机器和使用这些 Dockerfile 的 CI 必须具有
`base` 下所需仓库的读取权限；镜像同步操作才需要相应写入权限。

本机使用现有 Docker credential helper，`regctl` 可读取该配置；没有登录时使用：

```sh
docker login docker-hub.f123.pub
```

不要把密码放在 Dockerfile、build args、命令行参数或镜像锁定清单中。
Woodpecker/GitHub Actions 的现有 GHCR 登录不能替代内部 registry 的登录。
本次不修改 registry 的匿名访问策略，也不把发布凭据发给不可信 PR；后续 CI
接入须配置隔离的只读身份。
Woodpecker 构建步骤自身使用的 Kaniko 工具镜像同样不能从上游 registry
拉取；当前 test runner 为 amd64，因此该工具镜像使用内部
`docker-hub.f123.pub/devops/kaniko:v1.14.0-debug` 并固定 digest，记录在
[`scripts/ci/tool-images.lock.json`](../scripts/ci/tool-images.lock.json)。这不是
Dockerfile 输入，不改变上方基础镜像必须保留完整平台集的规则。

基础镜像同步并不意味着完整离线构建：Dockerfile 默认 apt 与 Bun/npm
源已按目标网络改为清华/npmmirror 镜像，PyPI 源改为 f123 内部镜像。
Woodpecker 使用的 `Dockerfile` / `Dockerfile.lite` 不再安装 Rust toolchain，
也不运行会访问 GitHub/spaCy 的离线缓存下载助手；API 启动必需的
tiktoken `o200k_base` / `cl100k_base` BPE 缓存以已校验文件提交到
`docker/tiktoken-cache/` 并随镜像复制，避免运行时访问 OpenAI blob 卡住。
未镜像的上游服务以及 `Dockerfile.postgres` 的 AGE 源码下载仍需受控构建出站访问。
运行时模型连接与本次基础镜像来源调整无关。

## 在本机同步，而非在流水线补齐

使用原生 `regctl` 在本机进行 registry-to-registry copy，无需把全部镜像导入
Docker Desktop VM。macOS 可通过 `brew install regclient` 安装。
工具用法见 [regctl image copy](https://regclient.org/cli/regctl/image/copy/)
和 [Docker 凭据读取说明](https://regclient.org/usage/regctl/)。

不能用普通的本机 `docker pull` → `docker tag` → `docker push` 代替完整同步：
需要保留源 index 中的所有平台和子 manifest，而不只是当前机器的 arm64 镜像。
`regctl image copy` 不指定 `--platform`，并使用 `--force-recursive` 检查/复制嵌套内容。
这保留 index 中的 attestation manifests；独立于 index 的签名/referrer 不在本记录的验收范围。

### 复核已锁定镜像

从仓库根目录运行，读取目标的实际 digest，不需要打印任何凭据：

```sh
python3 - <<'PY'
import json
import subprocess
from pathlib import Path

lock = json.loads(Path("scripts/ci/base-images.lock.json").read_text())
for item in lock["images"]:
    actual = subprocess.check_output(
        ["regctl", "image", "digest", item["mirror"]], text=True
    ).strip()
    if actual != item["digest"]:
        raise SystemExit("Mirror digest mismatch: " + item["mirror"])
    print("Verified:", item["mirror"], actual)
PY
```

### 更新基础镜像

1. 在本机解析新源镜像的完整 index digest，保存源 manifest 与平台清单；不要在
   同步过程中反复使用可能移动的源 tag。
2. 选择新的独立内部标签，确认不存在内容冲突，再执行：

   ```sh
   regctl image copy --force-recursive "$SOURCE_REPOSITORY@$SOURCE_DIGEST" "$MIRROR_TAG"
   regctl image digest "$MIRROR_TAG"
   ```

3. 确认目标 index 与源 digest 相同，逐个校验子 manifest 的摘要，并检查它们引用的
   config/layer blob 都可读取。源包含的 amd64、arm64 和其他平台必须完整保留。
4. **目标镜像已推送且验证完成后**，才更新锁定清单和三个 Dockerfile 的对应引用。
   CI 不负责拉公网镜像补齐，也不允许失败后回退公网源。
5. 运行 `./scripts/test.sh tests/setup/test_docker_base_images.py`，以及以下检查。
   本机 Docker driver 不支持一次请求多个平台，因此逐平台执行，不修改 Docker
   Desktop 的镜像存储设置或启动其他 builder：

   ```sh
   for dockerfile in Dockerfile Dockerfile.lite Dockerfile.postgres; do
     for platform in linux/amd64 linux/arm64; do
       docker buildx build --check --pull --platform "$platform" -f "$dockerfile" . || exit 1
     done
   done
   ```

   `--check` 验证 Dockerfile 和镜像元数据解析，不执行所有 RUN，不能当作完整应用构建。
6. 提交并推送到 fork `master`；不修改 `main`。旧镜像/tag 的保留和清理由 registry
   管理员处理，不在同步操作中自动删除。
