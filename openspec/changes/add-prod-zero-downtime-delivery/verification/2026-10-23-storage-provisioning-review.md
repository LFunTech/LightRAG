# 首次迁移补全的独立审查与修复

日期：2026-10-23；执行 executing-plans 的一次 fresh-context、只读整体审查；不额外派实现/复审代理。范围为本轮 provisioning 与生产启动接线、打包和文档，不以此前大面积脏工作区的全部 diff 为本轮成果。

## 审查结论（修复前）

- Strengths：PG迁移/授权/账本同事务，session锁跨图与登记；POST前持久意图与跨连接验证；生产maintenance只读、release关闭兼容；Job固定digest/已有引用，wheel资源完整。
- Critical：无。
- Important：4项，修复前 verdict 为 With fixes。不存在待递延 Minor。

| 原发现 | 后果 | 一次修复及本地证据 |
| --- | --- | --- |
| 空域登记沿用 `hasLabel(label)`，与既有现场已验证失败的保留名冲突 | apply/verify后register仍会失败 | 改成已验证的 `vlabel`/`elabel`；`test_empty_scope_uses_previously_validated_nonreserved_bindings` 精确核对HTTP脚本/bindings/aliases；engine两条登记用例不再跳过真实MockHTTP空域逻辑 |
| DSN path用lstrip删掉全部前导/，与asyncpg仅删一位不一致 | `//prod`被当prod批准，实际可访问另一个`/prod`库 | 精确保留第二个/、按驱动单层解码；最终同时拒绝缺省/0端口以避免环境PGPORT改变目标（独立1 RED→GREEN）；`test_repeated_slash_database_path_does_not_match_approved_database` RED→GREEN |
| graphspace/graph允许点路径，HTTP规范化改变实际域 | 可能调用默认图域的旧接口 | plan/GraphProvisioner共用纯验证，对齐runtime的非空、点路径和控制字符规则；4项dots/空白/DEL用例RED→GREEN，无网络 |
| 结构指纹遗漏RLS/policy/user-trigger语义 | 验证兼容却看不到数据/写失败，甚至误判空域 | snapshot明确拒绝本v1基线不支持的RLS、FORCE RLS、policy及任何用户trigger；4个实际隔离PG变更用例RED→GREEN，不自动修复 |

首轮审查RED日志10 failed/23 passed，图名后两条最初误按更窄规则要求拒绝编码字符，已改为真正runtime规则的空白/DEL并再次观察4 RED；不把测试规则失配算生产缺陷。修复后联合组210 passed/2既有弃用warning（19.61s）；修改登记fixture为实际MockHTTP后再跑最终组，结果见[最终验证](2026-10-23-storage-provisioning-local.md)。未执行真实图DDL。

## Declined to judge及执行者Ruling

1. 历史全部脏改动未重审：本轮范围不包含全部历史功能。Ruling：维持限域审查、跑受影响旧目录/发布集成作回归 — 不能把其他作者/旧功能归成本轮 — 未覆盖的历史缺陷仍可能存在，不能声称仓库全量审查通过。
2. 生产PG17.6/图1.7真实DDL、ACL、镜像运行和容量不判断：仅允许本地。Ruling：不以审查代替生产验收或新增权限 — 避免越权写环境 — 真正上线仍被这些未知项阻断。
3. 旧coordination测试count==2不作为本轮生产缺陷：已有3版迁移。Ruling：保留原SQL/checksum、把单测改成全版本/checksum核对 — 修复陈旧断言而不弱化版本门禁 — 完整迁移语义仍需实际场景验收。

不进行二次代理复审；4项均由执行者补RED→GREEN并重跑整组闭环。无commit/push/tag、集群/密钥/后端变更或生产gate解除。
