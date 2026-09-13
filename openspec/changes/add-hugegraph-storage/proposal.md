## Why

当前仓库没有 HugeGraph 图存储后端。用户已批准新增独立后端的方案，使用本机 HugeGraph 1.7.0 联调，不接入或改造已有业务图。

## What Changes

- 新增异步 `HugeGraphStorage`，完整实现图存储、批量读写、图浏览及有界维护迭代。
- 使用版本化独立 schema、workspace/namespace 隔离、稳定 ID 和单物理边表示无向关系。
- 保留属性类型和部分更新语义，错误显式上抛，保持核心恢复锚点与写入顺序。
- 接入后端注册表、环境模板、外部服务配置向导、文档和自动化测试。
- 使用本机隔离测试范围验证真实协议和 LightRAG 调用链，不删除已有业务 schema 或数据。

## Capabilities

### New Capabilities
- `hugegraph-storage`: HugeGraph 1.7 图存储适配、生命周期、隔离、协议安全与部署配置。

### Modified Capabilities
无。现有公开接口及其他存储行为不变。

## Impact

涉及 `lightrag/kg/`、`scripts/setup/`、环境模板、测试与文档。复用 `aiohttp`，不增加同步 SDK，不修改前端、API 权限、PostgreSQL/OpenFGA/Keycloak 状态。HugeGraph schema 仅新增专属版本化定义；不自动迁移其他后端数据，不修改现有业务图。用户批准日期：2026-09-13。
