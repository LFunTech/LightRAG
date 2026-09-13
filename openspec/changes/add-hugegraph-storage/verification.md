# HugeGraph 后端验收记录

日期：2026-09-13。范围：新增独立图存储，不读取或改造既有业务知识图谱，不迁移原 LightRAG 图。

## 需求覆盖

| 需求 | 实现与验证 |
| --- | --- |
| 可选后端与生命周期 | 三处存储注册、异步 client、1.7.x 版本检查、认证互斥、配置校验、释放与重初始化；注册和 client 测试 |
| schema 非破坏初始化 | 专属 3 属性、2 标签、2 索引；全量预检兼容性、并发创建冲突复查、任务等待和 verify-only；模拟协议及本机重复初始化 |
| 无向属性图和范围隔离 | 稳定哈希 ID、规范端点、标量类型、部分更新和批量顺序合并；单测、真实 CRUD 与 workspace/namespace 隔离测试 |
| 完整图契约 | 批量读写/删除/度数、标签及边有界游标迭代、导出；复用现有 8 个跨后端图契约，加自环契约 |
| 图浏览 | 搜索排序、含孤立节点的热门标签、Unicode 码点平局顺序、BFS 深度/节点上限与诱导边；单测及真实图验证 |
| 错误与并发 | 错误不变为空值、只读重试、无隐藏写重试、部分确认检查；共享 mutation 锁及发送前 pending fence；迟到写、取消、等价 URI、独立范围测试 |
| worker 死亡 | 真正 Manager 和 fork worker，在模拟传输入口 os._exit，不执行 finally；锁回收后新实例仍拒绝写、允许读；重建整个协调域后可重放 |
| LightRAG 产品路径 | 确定性 LLM/embedding 测试替身配合真实 HugeGraph；文档抽取入库、local/global/hybrid/mix 上下文检索、编辑、合并、共享文档删除、ACK 后调用方失败及人工重试 |
| 配置与文档 | env.example、外部服务 setup 向导、README/API 文档、专用部署恢复文档和可运行示例；setup 行为测试及示例 --help |

## 最终测试

```bash
HUGEGRAPH_URI=http://127.0.0.1:8080 PYTHON=.venv/bin/python ./scripts/test.sh \
  tests/kg/hugegraph_impl --run-integration -o addopts='' -q --tb=short
```

**197 passed**：137 client、39 storage、3 registration、1 worker-fence、8 integration、9 复用契约/自环。后 17 项连接本机 HugeGraph 1.7.0；其余不依赖外部服务。

```bash
PYTHON=.venv/bin/python ./scripts/test.sh \
  tests/setup tests/kg/test_batch_graph_operations.py tests/test_docstring_budget.py \
  -o addopts='' -q --tb=short
```

**402 passed**。首次附加跨后端批量测试因本地缺少可选 asyncpg 依赖出现 7 个导入失败；补齐虚拟环境的 asyncpg、neo4j、pymongo 后完整重跑上述子集通过，未因此修改其他后端代码或依赖清单。

两组最终测试合计 **599 passed**。未运行全仓库测试或前端检查（未改前端）。pytest 提示本机缺少 spaCy 模型，但上述最终子集没有跳过测试，不需要这些模型。

## 其他检查

- 新增/修改的 12 个 Python 文件：Ruff check 与 format --check 通过。
- 修改的 3 个 setup Shell 文件：Bash 5 的 `-n` 语法检查通过。
- `python examples/lightrag_hugegraph_example.py --help` 通过；未发起付费模型调用。
- `git diff --check` 通过；人工检查注册、脚本和新增模块；独立复审发现的 URI 等价拼写绕过已按 TDD 修复，收尾复审无阻塞项。
- `openspec validate add-hugegraph-storage --strict` 通过。
- 未 commit、push 或归档 OpenSpec；保留用户原有 `.dockerignore`、`.gitignore` 改动，没有修改当前 `.env`。

## 本机联调与安全边界

- 目标为 `http://127.0.0.1:8080`，`DEFAULT/hugegraph`，HugeGraph core 1.7.0。
- 测试只写入 UUID 专属 workspace/namespace；清理只调用该范围 `drop()`，未调用 graph clear、graph/schema 删除或服务重启。
- 对比联调前只读 schema 快照，原有 **58 项定义完全一致**。LightRAG 专属 schema 保留；测试结束专属标签顶点/边数均为 0。
- 实际服务验证促成两项协议修正：只保留顶点 scope/name 联合索引，避免 HugeGraph 隐式删除前缀索引；Gremlin 直接绑定原生列表，不使用被沙箱拒绝的 JsonSlurper。
- 只声明 HugeGraph 1.7.x 支持；实际服务验证为 1.7.0，不宣称其他版本经过实测。认证/TLS 参数和错误路径通过模拟 HTTP 测试覆盖，未另行部署带认证或 HTTPS 的 HugeGraph。
- 真正响应丢失/迟到提交通过受控故障模拟覆盖；真实集成中的恢复场景是 **ACK 已确认之后调用方失败**，不冒充真实网络丢包试验。
- 不确定写屏障是保守停写策略，不是跨整协调域重启的持久化日志。生产恢复须停所有 writer、确认无旧请求在途并审计，再重启整个协调域和人工重试；禁止只换连接、URI 别名或另起部署绕过保护。部署入口 URI 必须统一。
- 不替代 KV/向量/文档状态存储，也不提供跨存储事务；切换既有部署时须在新 workspace 受控重建。详细规则见 `docs/HugeGraphStorage.md`。
