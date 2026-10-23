# 本地验收准备包文档核验（2026-10-21）

本轮用户只授权补充OpenSpec文档；已形成[本地验收准备包](acceptance/README.md)，包括待确认参数、指标采集、全写入口/故障、隔离作业及停止预案。未知参数留空、全部执行批准为false；不是可执行load/Job配置，不代替真实验收。

## 核验范围

- OpenSpec scoped strict、全部相对文件链接及57场景/53任务一致性；主任务保持41完成/12未完成。
- 参数JSON语法、用户确认值和所有待定叶子为null，审批仅false/空编号，无真实密钥或目标值。
- 用Python AST读取源码字面集合和路由装饰器，不导入应用/认证/.env，核对19 HTTP、17 SDK、7后台kind与文档逐项一致及HTTP方法/路径。
- 清单覆盖20类指标、26故障用例；显式区分已有插桩/外部缺口、正常/拒绝/故障、四类工具动作/其他全入口适配。
- 本轮前后1120个非密钥源码/文档快照对比：只允许本change的OpenSpec文件变化，原代码/工具/测试/流水线/部署/普通文档不变；HEAD/branch与prod静态gate不变。
- 文档读者检查：针对执行资格、空参数、入口覆盖、指标口径、隔离与停止的8个问题做一次只读检查；结果在下文记录。

此处不重跑后端/前端或PG测试，不访问集群/既有数据库，不发压、不导入/读取真实密钥、不提交/push/tag。历史9420/70/254结果不作为本轮新增测试通过记录；OpenSpec planning artifacts done也不等同12项真实任务完成。

## 最终结果

| 校验 | 实际结果 |
| --- | --- |
| `openspec validate add-prod-zero-downtime-delivery --strict` | valid |
| `openspec instructions apply --change add-prod-zero-downtime-delivery --json` | ready，53项/41完成/12 remaining；不是all_done |
| `python3 temp/prod-delivery-implementation/verify-acceptance-preparation.py` | 198个null待定叶子、12类批准false；19 HTTP方法/路径、17 SDK、7后台kind与源码一致；20指标、26故障、57场景唯一映射通过 |
| 文件/任务/链接与边界 | 121个本地文件链接可解析；1120个既有文件快照中仅本change的5个OpenSpec文件变化；HEAD/branch、prod gate及非OpenSpec代码/测试/工具/部署/流水线/普通文档不变 |
| 读者测试 | 一次fresh reader回答8类问题，指出2处实质歧义（F24关闭资源范围、指标责任人映射）；修正后同读者仅复核这两项，确认解决。没有用读者审查签署真实容量/权限或执行授权 |
| 默认离线CLI对准备底稿的拒绝 | `query_load --plan .../acceptance/parameters.json` 返回ValueError/退出2；未带`--run`、未发送网络请求，证明不是可用负载计划 |
| 空白/JSON与`git diff --check` | 通过；未把未知值补成0/无限/测试默认，不修改原workload-target草稿 |

[机器记录](acceptance-preparation-results.json)保存审计摘要与准备包文件SHA，临时校验日志在仓库ignored temp/prod-delivery-implementation/acceptance-preparation-*。运行单、审批编号、资源/模型/SLO/隔离/真实质量与容量证据仍为空/未运行，所有现场执行/容量/质量/生产结论保持未验证。

本轮已覆盖用户要求的五类本地材料，不扩大为功能实现、新组件、执行授权或任务结项；后续先由用户/责任方确认底稿，再按隔离只读核验→资源/seed→低负载连续性→专项容量/故障→优化评审/对照→生产独立接入的门禁推进。
