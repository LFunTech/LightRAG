# K8s-only 首发：唯一审查与一次修复

- 审查基线：`3e153773d25345bb76fb001035b27867cb4aaab6`，不可变patch SHA256 `275f22c3f17358263467b039d542e4fd903700202d9343eaaadd7859fc039160`。
- 一名只读reviewer，0 Critical / 2 Important / 0 Minor；未读密钥、未调用环境。用户明确取消CLB/EO校验及批准首次共用专用账号，不作为缺陷重新加回门禁。

## 两项 Important

1. **pipeline attempt 可另建初始化Job**：scope固定Job名，先原子创建immutable execution claim绑定identity/settings。已有不同批准、claim存在但Job缺失一律停止审计，不重复迁移/图写。相同执行仅观察原Job。
2. **观察超时未约束kubectl耗时**：API request/subprocess有限timeout，阶段读写传剩余deadline，读取后的成功也复核预算。immutable创建的GET与POST共享同一预算，GET耗尽后不得POST。超时不杀远端Job/Pod、不自动recover/rollback。

一次fix pass：6项首轮RED→GREEN；补最末边界2项（immutable读预算1 RED，迟到startup成功1 GREEN）后2 GREEN。没有第二轮审查。

独立JSONSchema工具随后发现空network分支仍被旧minItems拒绝；增加明确K8s空network正例、混合空/非空反例及legacy非空要求，先1 RED后GREEN。不是用宽CIDR猜入口来源：本模式不创建NetworkPolicy，保留namespace已有策略。实际私有prod输入独立校验通过；含jsonschema的完整29项首发CI测试通过。

最终回归、隔离PG、静态/打包结果见[本轮验证](2026-10-23-kubernetes-initial-local.md)。本地模拟不是生产部署证明，实际流水线结果另记。
