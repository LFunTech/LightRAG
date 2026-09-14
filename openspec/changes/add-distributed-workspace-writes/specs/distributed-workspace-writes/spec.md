## Purpose

为相同 workspace 的多个独立 LightRAG 进程或 Kubernetes Pod 提供可验证的并发写入、来源完整性和持久化恢复阻断，避免重复调度、相互覆盖与旧请求迟到提交导致的数据丢失，同时保留默认单机兼容行为。

## ADDED Requirements

### Requirement: Explicit supported deployment profile
系统 SHALL 默认保留本地协调，并仅在显式启用且外部存储、共享文件路径和 workspace 配置满足约束时启用分布式写入；不匹配配置 MUST 在写入前拒绝，不得打印密钥。

#### Scenario: Unsafe storage or mismatched participant
- **WHEN** 分布式参与者使用文件 KV/向量、非共享路径声明或不同配置指纹
- **THEN** 启动明确失败且没有业务存储变更

#### Scenario: Kubernetes Service environment collision
- **WHEN** Helm 部署的命名空间存在名为 `postgres` 等 Service
- **THEN** 应用 Pod 与维护 Job 禁用隐式 Service-link 环境变量，显式 DNS/Secret 配置仍生效，数字端口不被 `tcp://...` 覆盖

#### Scenario: Existing local deployment
- **WHEN** 未启用分布式写入
- **THEN** 现有单机初始化、API、锁和存储行为保持兼容

### Requirement: Actual concurrent writes with conflict protection
系统 SHALL 允许同 workspace 不同资源在独立进程中同时执行持久化写入；同一文档、实体或关系的读改写 MUST 互斥或可靠检测冲突，不得丢失已确认来源、属性或恢复锚点。

#### Scenario: Parallel independent writes
- **WHEN** 两个独立进程分别提交互不冲突的实体
- **THEN** 两次业务写入能够时间重叠且都可读取，不经过唯一全局写进程

#### Scenario: Shared entity contributions
- **WHEN** 多篇文档由不同进程处理并贡献给同一实体/关系
- **THEN** 最终包含全部不同真实来源，权重满足证据数下界，重复投递不重复增加证据

### Requirement: Durable document ownership and discovery
系统 SHALL 在一致性修复或任何处理阶段之前领取文档，并持续保持有效所有权；通知丢失 MUST 可由持久化状态扫描恢复。FAILED MUST 只由显式人工请求授予一次新的尝试。

#### Scenario: Competing consumers
- **WHEN** 两个进程同时发现相同文档或一个分页全部被其他进程占用
- **THEN** 只有领取者处理该文档，其他进程继续查找其他任务，不修改被占用文档且不忙循环

#### Scenario: Lost notification and retry intent
- **WHEN** 通知丢失或提交人工重试后请求进程退出
- **THEN** 已提交任务/重试意图不丢失，普通轮询不自动重试 FAILED

### Requirement: Persistent uncertain-write fence
系统 MUST 在实际存储 mutation 前持久化待确认状态，仅在确定有效成功后完成；超时、取消、响应异常、进程死亡或协调器重启 SHALL 不自动解除未确认屏障。失去协调库连接 MUST 拒绝后继写而非降级。

#### Scenario: Acknowledgement loss and replacement
- **WHEN** 写入可能已提交但响应丢失，随后全部应用进程重启
- **THEN** 后继冲突写及删除仍被拒绝，未确认记录可查询，读诊断不删除数据

#### Scenario: Paused or killed writer
- **WHEN** writer 在发送前后暂停或被强制终止，另一个 writer 尝试接管
- **THEN** 不因锁释放、心跳过期或超时就允许危险覆盖，持久化所有权/屏障保留

### Requirement: All mutation entry points honor maintenance exclusion
系统 SHALL 将 SDK、HTTP 及后台任务的入库、自定义数据、图编辑、删除清空、扫描重试、冲突修复、缓存写和启动迁移纳入协调；维护独占期间 MUST 拒绝或有界等待其他变更。

#### Scenario: Clear versus ingest
- **WHEN** 一个进程正在入库，另一个请求清空/删除或启动迁移
- **THEN** 二者不交错破坏数据，维护获得独占前不修改文件或任何存储

#### Scenario: SDK or detached background mutation
- **WHEN** 调用不经过 HTTP preflight 或后台任务在请求结束后执行
- **THEN** 仍需获得有效持久化许可，不能复用已结束请求的许可绕过保护

### Requirement: Recoverable multi-store progress
系统 SHALL 保留多存储操作阶段和现有来源锚点，使已确认部分提交可重放收敛；不得把未确认写当作未发生，不得以删除 attribution carrier 掩盖失败。

#### Scenario: Failure between graph and vector commits
- **WHEN** 图、KV、向量或文档状态之间发生失败
- **THEN** 操作不是伪成功，恢复所需记录仍存在，受控恢复后数据一致且不重复累加证据

### Requirement: Auditable explicit recovery and migrations
系统 SHALL 提供版本化幂等迁移、只读诊断及保留历史的显式恢复；恢复 MUST 要求运维确认旧 writer 停止、旧请求结束和已提交状态审计，不得自动删除协调历史或业务图。

#### Scenario: Recovery without prerequisites
- **WHEN** 未提供要求的运维确认就请求恢复，或应用启动发现 schema 未准备好
- **THEN** 操作拒绝且屏障/数据保持不变

#### Scenario: Controlled recovery
- **WHEN** 完成停止、静止与审计确认后执行恢复
- **THEN** 历史记录保留、恢复审计可查，新的有效操作可以执行，旧应用许可不能重新使用
