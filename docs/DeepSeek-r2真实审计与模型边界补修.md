# r2 核对：只补真实审计链与服务模型边界

Codex 当前源码核对发现以下确定问题，尚未部署、未调用模型。继续原授权，不必询问是否修；本轮离线完成后停止。

## 1. 0.4 审计分支不可达与回执路径遗漏

`ae_cloud_re_multiturn_input_audit.py:626` schema允许元组仍只有0.1/0.2/0.3，0.4先报wrong_multiturn_schema，644行新增分支无法到达。704行multicall分支和731行stack_evidence_gate选择同样未包含0.4。须使0.4沿真实入口完成既有multicall/stack证据核验，不是仅将schema加入列表。

同文件 `_deepseek_capacity_gate` 无记录分支引用未定义的 `report`（约930行），应得到稳定AuditFailure而不是NameError。核验真实记录来源；不能用任何非空hash字符串当作已完成请求绑定。

验收：调用公开 `audit_re_multiturn_inputs`，用完整、真实字节关系的离线0.4完成态fixture到达最终判定；保留所有真实审计门，不mock前面的schema/传输/回执校验。合法链通过，缺gate/错hash/缺或篡改stack receipt分别失败；旧0.3回归保留。只调用新私有方法不算贯通。

## 2. 服务字节策略目前不是模型专属

`ae-no-compaction-letta.patch` 的 `_ae_capacity_gate` 只看 `_ae_byte_gate_policy()`进程环境，有声明就对所有带禁摘要标签Agent切字节门；从未核对agent的llm_config模型或request_data模型。故“其他模型仍走旧Qwen门”目前不成立。

绑定明确的DeepSeek模型身份/策略，在真实服务方法里核对声明与请求身份一致；非目标或矛盾声明不得进入byte-only分支，不能默默跳过旧计数约束。保持无声明旧路径。非法/半声明要明确拒绝而非静默回退可能有计数器的旧路径。同步launcher传参、现有manifest与receipt涉及的改动。

验收执行真实服务方法体：合法DeepSeek请求进入byte门且零Qwen计数；同进程声明下Qwen/未知模型不被byte策略放行；超预算零发送；旧路径无可信计数仍拒绝。

## 3. 更正交付说明，不增加框架

旧回执固定指向 `transfers/ae-stack-startup-20260914-r1/`：未来部署新服务也不会改变这个封存文件，所以“部署后该测试自然变绿”不成立。按测试实际目的区分历史快照一致性与当前补丁fixture，不能覆盖历史回执；必要时另建当前fixture并保留旧回执对新树拒绝的反例。这不是要求扩大测试体系。

请使用新独占交付目录 `results/ae-deepseek-re-multiturn-integration-r3/`，不要再更新已交付r2；只附本次差异、相关链测试日志、部署增量。不要将独立函数/CLI PLAN通过描述成CLI到事后审计完整通过。本次不扩展实验、网络探测或重验全部旧交付。
