# 首发共用专用账号：唯一最终审查与修复

基线`601595513c62de3d842ac95b7aebc27e641f7d7a`，不可变审查补丁SHA256 `09ef84cfcd652f94b9ff6d489092e061e3e4b639eb934d20560a26310af6c507`。按executing-plans，仅一名fresh-context reviewer只读审查，不接触网络、集群、密钥或工作区写入；未派复审。

## 原始结论

**With fixes：0 Critical / 3 Important / 0 Minor。** reviewer认可默认身份分离不变、集中批准解析、PG当前/session真实用户及高权限成员审计、普通发布与Green拒绝、迁移SQL/test流水线不变且无K8s TLS。未将已获用户批准的共用owner风险或明确未实施的首发流水线/云环境验收当成本补丁缺陷。

| Important | 经调用链核对的根因 | 同一fix pass的修复与证据 |
| --- | --- | --- |
| 初始化写入未检查持久首发状态 | quiescent允许COMPLETED等闲置后续状态，原apply可能继续GRANT；仅在enroll/runtime核验批准太晚 | `CredentialException.verify_initialization_state`允许尚未enroll，或准确相同的Blue/epoch1/IDLE/open/no-owner；engine apply/register、PG migrate/grant及图apply在副作用前接入。3种漂移×5入口15 RED→GREEN；真实隔离PG中将scope改为epoch2/Green/COMPLETED后apply拒绝、图POST仍7次 |
| enroll未绑定operator DSN物理目标 | 配置environment的hash及PG同名数据库/用户不能证明连接host/port一致 | enroll连接前加载/核验配置与环境，复用初始化DSN精确host/显式port/database/user校验；替换host或port两项RED→GREEN且connect为0，另补准确目标实际CLI分支接线/持久policy正例 |
| Job自动替操作者确认 | 仅environment含批准便自动加入额外执行flag | 可选`settings.confirm_shared_credentials`默认不出现，不改旧Job契约；共用apply/register须显式true，缺失/false拒绝，非法类型及无批准拒绝。readonly verify不需也不生成执行flag。4项缺失/false测试RED→GREEN，补类型/readonly/无批准5项回归 |

21项反例先观察失败，再修产品。首次修复过程中自动缩进编辑产生IndentationError（19 passed/2 failed），立即修复语法后21 passed；未将语法错误当行为RED或合格验证。其后补正例及真实PG漂移，重新运行全部相关组，最终数字见[验证记录](2026-10-23-first-shared-credentials-local.md)。

## 自行验收结论

三项Important均完成修复与回归，无递延Minor。只读verify允许检查后续状态，但任何共用例外写入口不允许将首发批准重用于后续版本；不会创建角色或回滚/删除远端状态。新enroll目标校验对默认独立身份同样生效，不改变release关闭的test/local路径。

本结论只覆盖本地补丁；尚未部署prod、未签署真实初始4Pod/4+4、云入口/对象生命周期或1000QPS。初始流水线编排缺口和合格EO/CLB只读引用仍独立存在，不以通过本轮审查豁免。
