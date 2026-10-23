# 已批准首发共用专用账号：实现与本地验证

日期：2026-10-23。用户明确确认首次稳定上线可复用现有LightRAG专用PG/HugeGraph账号作为运行与迁移身份；**不再将缺少第二套角色或CREATEROLE作为此次首发阻断**。仅豁免身份分离，保留版本化migration、verify-only启动和其他生产门禁。基线`601595513`。

## 已实现边界

- 默认无例外仍采用独立最小权限模式；新增完整的version=1批准对象，绑定approval_id、scope、physical profile、正式tag、PG用户名/数据库及图用户名。拒绝畸形、缺项、跨目标/版本与风险未确认；配置hash及migration plan hash包含例外。
- `apply/register`保留原确认并要求`--confirm-shared-credentials`；Job的`settings.confirm_shared_credentials`也必须显式为true，不从静态风险批准自动生成，只读verify不要求该执行确认。迁移DSN、图迁移用户名/密码仍须显式提供，不从runtime默默兜底；只允许与批准一致的共用账号。实际PG核验`current_user`、`session_user`、目标库，拒绝superuser、CREATEROLE、CREATEDB、复制、BYPASSRLS和高权限成员；不创建角色或修改旧迁移SQL/checksum。
- migration/verify/register仍验证版本、profile、结构和所需CRUD权限；真实release启动核验schema、批准及持久scope状态。初始化所有写入口还在GRANT/DDL/图操作前检查持久scope尚未enroll或准确同一首发状态，后续epoch/Green/批准漂移拒绝；enroll在连接前绑定DSN host/显式port/database/user。批准写入已有release policy，仅首个Blue/epoch1/IDLE/open且无owner可运行；同首版同批准可重建。
- 普通`prod_deploy`、`deliver`和持久store升级入口拒绝例外，不能用于Green、后续tag或普通回退。后续4+4前需单独整改身份/权限，不能删policy冒充已恢复隔离。
- 共用owner可DDL和修改release控制表，是已接受风险，**不宣称DB角色隔离**。K8s运行/operator引用继续分开，避免云控制凭据进入应用；不引入K8s TLS、Secret导入或角色创建。

## 验证

| 验证组 | 实际结果 | 范围 |
| --- | --- | --- |
| `tests/ci tests/distributed tests/provisioning tests/api/config` | **646 passed / 144 skipped / 2 warnings**，5.33s | mock/离线，集成默认跳过 |
| 私有本地PG runner：`tests/provisioning tests/distributed/release tests/ci/test_prod_shared_credentials.py --run-integration` | **240 passed / 2 warnings**，8.00s | 仓库temp独立PG、随机loopback端口；真实PG专用无特权owner，图为MockHTTP |

两组重叠，不累加；warning均为既有websockets弃用。本地PG实际走首次apply、重复apply、verify、register及release policy持久化/同批准重复apply/实例初始化和后续epoch拒绝，图HTTP边界验证7次schema POST，不代表真实HugeGraph写入通过。测试由本地admin准备隔离角色；产品未新增CREATEROLE需求。

TDD日志保留于ignored `temp/prod-first-shared-20261023/`：策略24 RED、provisioning3 RED、runtime9 RED、raw-store1 RED、普通pipeline1 RED及session_user1 RED均观察后转GREEN。首次本地PG受sandbox shmat限制未启动；另两次失败为新测试夹具，已修复并重新运行完整联合组，没有放松产品边界。PG已由trap停止，`pg_ctl status`再次确认无运行server。

20项受影响Python的Ruff/format、5份Woodpecker strict lint、prod shell语法、OpenSpec scoped strict、68场景证据行及受影响文档链接检查通过。原test workflow/脚本/overlay、构建workflow、迁移SQL/checksum无diff，prod静态gate仍false。干净源码导出1673文件，wheel/sdist成功构建，各17项源码/资源逐字节相同且无秘密目录；hash见[机器证据](2026-10-23-first-shared-credentials-local.json)。本地包不是新生产镜像或正式发布tag。

唯一fresh-context最终审查得到0 Critical/3 Important/0 Minor；三项均经21项RED→GREEN及同一fix pass修复，增加准确目标enroll/readonly Job等正例并重跑完整两组。未派复审，见[审查与修复](2026-10-23-first-shared-credentials-review.md)。

## 当前仍未上线的准确原因

1. **实现侧**：首次环境没有运行中的Blue，必须补显式migration→verify→register/enroll→初始4×1 Blue→入口/逐Pod验收的流水线串接；普通runner是升级路径，不能伪造健康Blue来复用。
2. **现有云输入**：最后一次实际核对中，prod清单云身份对目标CLB返回UnauthorizedOperation、EO可见站点数0；浏览器已保存EO配置和DNS，不等于CI已取得只读API观测/审阅hash。需合格只读引用或另行授权准备IAM，不借用其他业务/控制器凭据。
3. **真实验收**：S3对象ACL/写读删、应用首发、资源/预算和公网TLS/长流仍未验；1000QPS/SLO未通过。未知参数继续为空，不把单测fixture当批准值。

新增第12节6项本地任务完成，当前**53/65，仍原12项真实任务未完成**；原12项不勾；此轮未连接/变更prod、原test或两个候选，未迁移、部署、切流、发压、recover、重放、导入密钥或创建tag。Git交付如执行，单独记录，不把提交或CI构建当上线。
