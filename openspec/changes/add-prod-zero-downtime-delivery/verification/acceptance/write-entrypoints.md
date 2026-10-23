# 完整写入口与验收覆盖清单

这是**代码清单，不是执行许可或真实验收结果**。截至2026-10-21，所有下列远端用例均为`not_run`，结果/证据位置留空；原本地单测结果保留在[场景证据矩阵](../scenario-evidence.md)，不复制为远端通过。

源码权威：[HTTP WRITE_ROUTES](../../../../../lightrag/api/release_commands.py)、[SDK_WRITES/BACKGROUND_OPERATIONS](../../../../../lightrag/distributed/release/runtime.py)、[documents路由](../../../../../lightrag/api/routers/document_routes.py)、[graph路由](../../../../../lightrag/api/routers/graph_routes.py)。路径为未挂载前的路由，实际root_path需另核验；不得根据HTTP方法把只读POST误当写入。

## 每个可写入口都要满足的共同断言

- 开闸正常路径保留原验证/响应语义；闭闸竞态必须二选一：已获ticket且纳入旧写排空，或首次业务变更前进入持久队列。不能在等待时持业务锁/事务。
- 在DRAINING/READ_SWITCHED采集真实QUEUED身份；交权后确认同主体/scope/key的同UUID终态。新命令由Green唯一有权执行；Blue迟到写不得取得新票，不以服务路由撤销代替写权校验。
- 原同步响应、异步持久受理、业务处理终态**分别验收**；超时/断开/EXECUTING/UNCERTAIN不代表未执行。确认同key同payload无重复效果、异payload拒绝，禁止重放未知写。
- 数据范围只能是本run专用fixture；保留原doc-status、chunk、图/向量及归因anchor证据。删除后doc消失不证明跨存储原子性，按[原purge合同](../../../../../docs/design/PurgeRecoveryContract.md)核对可接受残留/恢复路径，不手工补metadata、清锁或删anchor。
- 父/子后台ticket、主体撤销、清理竞态、查询持续性和原test默认分支都要在对应场景取证。实际不支持/配置关闭的入口应证明拒绝且无副作用，不能静默删出清单。

## HTTP：19个已登记route identity

“现有工具”仅指10.7自动动作，不表示现场已验；“补用例”需未来另行授权/实现受控适配，不能用任意hook替代。所有case结论和证据当前空白。

| ID | route.name | 方法与路径 | 预期证据/工具边界 | 结论/证据 |
| --- | --- | --- | --- | --- |
| H01 | `presign_object_upload` | POST `/documents/uploads/presign` | 会话/对象作用域、续签响应不持久化预签名URL，未完成引用受保护；补用例 | |
| H02 | `complete_object_upload` | POST `/documents/uploads/complete` | 有效对象/会话、enqueue持久受理和最终处理分别确认；跨过期/清理竞态；补用例 | |
| H03 | `scan_for_new_documents` | POST `/documents/scan` | 当前prod profile关闭本地文件入库，应拒绝而不扫描；不同profile正例必须先单独批准 | |
| H04 | `repair_source_conflict` | POST `/documents/source_conflicts/repair` | 仅专用冲突fixture、修复结果/归因及独占语义；不是停止预案的自动修复，补用例 | |
| H05 | `upload_to_input_dir` | POST `/documents/upload` | 当前prod profile关闭本地文件入库，应拒绝无副作用；不得为测试改test/打开未批准profile | |
| H06 | `insert_text` | POST `/documents/text` | 现有`insert_text`动作；source用保留run身份的basename，track/doc/source核对直到processed | |
| H07 | `insert_texts` | POST `/documents/texts` | 批量大小/原受理结构、各文档终态、失败/重复项与独立后台ticket；补用例 | |
| H08 | `clear_documents` | DELETE `/documents` | 仅一次性可销毁fixture、全域破坏性用例需额外批准；不能在容量run或停止时清空 | |
| H09 | `delete_document` | DELETE `/documents/delete_document` | 现有`delete_document`动作仅删本run已确认insert，delete_file/cache=false；异步删除追踪至终态并补跨库证据 | |
| H10 | `reprocess_failed_documents` | POST `/documents/reprocess_failed` | 专用FAILED fixture，每次人工意图只授予一次尝试，不能扩大重试范围；补用例 | |
| H11 | `force_reset_recovery` | POST `/documents/recovery/force_reset` | distributed分支按原路由拒绝409；只准备拒绝/无副作用用例，**禁止正向force_reset/recover/清fence** | |
| H12 | `cancel_pipeline` | POST `/documents/cancel_pipeline` | 只验专用业务取消语义；不能作为发布/停止时强行排空手段，补用例 | |
| H13 | `update_entity` | POST `/graph/entity/edit` | 现有`edit_entity`仅改本run实体描述，禁rename/merge；rename等需独立归因用例 | |
| H14 | `update_relation` | POST `/graph/relation/edit` | 先校验完整关系/权重证据，图/向量/归因读回；补用例 | |
| H15 | `create_entity` | POST `/graph/entity/create` | 现有`create_entity`用run前缀，图读回；实体/向量一致性另补证据 | |
| H16 | `create_relation` | POST `/graph/relation/create` | 仅专用实体间关系，source/weight合同、图/向量读回；补用例 | |
| H17 | `merge_entities` | POST `/graph/entities/merge` | 专用合并/重名fixture，来源、关系weight和恢复anchor按原合同核对；补用例 | |
| H18 | `delete_entity` | DELETE `/graph/entity/delete` | 仅专用实体、图/关系/向量及归因结果；补受控破坏性用例 | |
| H19 | `delete_relation` | DELETE `/graph/relation/delete` | 仅专用边、图/向量与归因结果；补受控破坏性用例 | |

## SDK：17个已登记方法

每项都需用实际SDK调用验证，不能拿同名HTTP测试替代。必须有`request_context(service_id,idempotency_key)`且service列入批准writer清单；无上下文/未知service先于业务变更拒绝。参数只允许安全版本化JSON/S3引用，文件句柄/回调/临时路径不能入队。同步便捷包装器应追溯到其async入口，不产生第二套写身份。

| ID | 方法 | 特有取证点 | 结论/证据 |
| --- | --- | --- | --- |
| S01 | `ainsert` | 持久入队/处理状态，source/文档身份与一次效果 | |
| S02 | `ainsert_custom_chunks` | 专用chunk更新、原patch journal/anchor，失败不能绕过原恢复合同 | |
| S03 | `ainsert_custom_kg` | 自定义实体/边完整校验，source/weight与图/向量/anchor结果 | |
| S04 | `aclear_cache` | 仅额外批准的一次性fixture；不得清抽取恢复证据制造冷态或停止压力 | |
| S05 | `adelete_by_doc_id` | 原DeletionResult与purge证明，非本run数据不得删除 | |
| S06 | `adelete_by_entity` | 专用实体删除语义、图/向量/归因结果 | |
| S07 | `adelete_by_relation` | 专用边删除语义、图/向量/归因结果 | |
| S08 | `aedit_entity` | 专用描述/rename分开case，按原merge/rename合同验证残留 | |
| S09 | `aedit_relation` | 关系weight/来源在首次mutation前校验 | |
| S10 | `acreate_entity` | 专用实体与向量读回 | |
| S11 | `acreate_relation` | 专用关系、非负weight及来源下界校验 | |
| S12 | `amerge_entities` | 专用合并的图、向量、来源/weight与恢复anchor | |
| S13 | `apipeline_enqueue_documents` | 原doc-status持久受理、source身份、无重复文档 | |
| S14 | `apipeline_enqueue_error_documents` | 原失败记录/metadata，不能意外自动重试 | |
| S15 | `apipeline_request_retry` | FIFO/sticky人工意图、一次尝试与确认 | |
| S16 | `apipeline_process_enqueue_documents` | 领取批次、闭闸未领取、交权后恢复；不能以方法返回代替全部文档完成 | |
| S17 | `apipeline_reset_failed_for_scan` | 扫描专用fixture的FAILED重置/排他；不能变成全库自动重试 | |

## 后台：7个操作kind

| ID | kind | 特有取证点 | 结论/证据 |
| --- | --- | --- | --- |
| B01 | `pipeline` | 每批领取准入，闸门关闭后不新claim；已获准批次正常完成 | |
| B02 | `manual_retry` | 不扩大原人工retry次数，sticky请求不因换Pod消失 | |
| B03 | `pipeline_index_file` | 持久源引用/文件作用域，独立ticket、enqueue确认 | |
| B04 | `pipeline_index_texts` | 多文本持久受理、独立ticket，不借已完成父命令票 | |
| B05 | `run_scanning_process` | 仅获准profile/fixture；本地文件关闭时不意外启动扫描 | |
| B06 | `background_delete_documents` | 删除受理与实际清理分离，独立ticket与purge终态 | |
| B07 | `scan_processing` | 专用扫描/分类/执行边界，闭闸不新领取，已有批次不被强杀 | |

## 隐式写、初始化和未登记入口

| ID | 类别/入口 | 必须证明的边界 | 结论/证据 |
| --- | --- | --- | --- |
| I01 | 查询/关键词cache填充 | 旧slot/standby/闭闸发送前可观测skip；已发送错误不吞、ticket纳入排空；无答案cache策略不读旧答案 | |
| I02 | 抽取cache、chunk引用与anchor写 | 属业务归因，不能当可丢弃查询cache；保留引用先于行/commit与原purge恢复合同 | |
| I03 | 上传会话/对象/结果的清理 | 未完成command引用受保护；入队和清理同scope排序；过期不自动丢源 | |
| I04 | provider及存储的在途请求、迟到ACK | 全部后端生命周期可追踪，超时/断开不记为未执行，旧slot无权补写 | |
| I05 | 必要query计量/审计 | 独立可靠authority保存/确认/迟到usage；未知则拒绝release接入，不按cache skip | |
| U01 | `arollback_failed_custom_chunk_patches` | 有operation_guard但不在SDK_WRITES；无有效父ticket的直接外部写应拒绝，不因方法存在就推断支持队列重放 | |
| U02 | 直连storage/数据库/图/S3、导入/修复脚本、外部SDK | 不在受控名单不得参与；调查实际writer清单。原始修复/回填不得挂在普通发布或停止流程 | |
| U03 | `initialize`、`initialize_upload_sessions`、migrate/enroll/bootstrap/recover/离线repair | 初始化只走原verify-only/明确允许操作；DDL/接入/恢复独立审批。日常发布与演练禁止自动bootstrap/recover | |

只读`aquery/aquery_data/aquery_llm/release_read`及`query_text/query_text_stream/query_data/generate/chat/get_documents_paginated`不因POST方法而入业务写队列，但其副作用仍受I01–I05约束。普通业务身份不得修改release owner/epoch/slot/gate；相关控制负例见F19。

## 覆盖闭环

逐项填写位置已建立在[执行台账](execution-ledger.json)，规则见[登记说明](records-guide.md)；全部51入口保持not_run，适配/批准/结果/证据为空，不能据本地行数完整推断真实覆盖。

为每个ID填写实际profile、批准范围、phase/故障case、私有fixture及输入hash、原响应/终态、效果次数、查询/归因/权限结果、证据引用和评审人；列出`not_run`/`passed`/`failed`/`not_applicable`及理由。不适用仅凭现场配置和拒绝证据签署，不能由本包代填。H08/S04/U03等破坏性或维护操作即使出现在完整清单，也不授权执行。
