# 容量预检与禁止摘要 r1：定向补修任务

日期：2026-09-14

## 目标与停止点

r1 定向核验未通过。请先修正容量结论，再修最小保护实现；交付到本地离线候选后停止。先读项目 `AGENTS.md` 与原任务书 `docs/DeepSeek-原始多轮容量预检与禁止摘要改造任务.md`。

本轮不 SSH、不部署、不调用付费模型、不执行真实 RUN，不改模型、任务顺序、R/E 定义、评分、输出预算或工具回包上限。保留本臂完整累积历史，禁止摘要、裁剪或重置。旧 r1 交付及所有 raw/transfers 不改，新交付用独占 `results/ae-multiturn-capacity-no-compaction-r2/`。不重复全仓回归。

## 已核事实与核验边界

- Codex 从 r1 目录解析 SHA256SUMS，42 项摘要全部一致。这证明交付内部字节一致，不代表实现正确，也不是重新验证所有历史产物未变。
- 真实摘要前请求为序列 161，provider prompt 61494、total 61524；摘要器自己的输入 24052 不混用。
- Codex 复跑 `.venv-vita/bin/python -m pytest tests/test_ae_no_compaction.py -q`：29 passed、2 failed。失败涉及缺失 `/tmp/ae-letta-QaM3ld/letta-v1`，继而出现 `no_compaction_tag_absent_from_the_created_agents`。这是当前复现入口失败，不直接推断生产逻辑失败；请提供可复现的固定源码路径及环境命令。
- compact_messages 的源码级保护测试通过；尚不能据此认定启动、计数、三角色、加载凭据和审计整链通过。

## F1：容量累计公式违反保留历史协议

位置：`ae_multiturn_capacity.py::projection_layer`，当前公式：

```python
total = total + task["history_tokens"] - previous_history + growth
```

上一任务的 history envelope 应留在上下文，此处每轮减掉旧历史，实际上按替换历史估算。固定历史总数 83355，却报告 t12 floor 64044，不能用于容量决策。

补修要求：

- 从真实封存请求核对锚点究竟位于 t4 结束还是 t5 已注入历史之后；不要把序列 161 自动称作 t4 末尾。当前分类 history=14439，恰为已报 t4 6319+t5 8120，需要核真实消息来源与阶段。
- 明列锚点已包含哪些材料，后续只追加尚未计入的材料，既不减旧历史，也不重复加入已包含的 t5。
- 累计内容涵盖实际持久保留的历史、任务指令、动态对话；工具 schema 按各阶段真实请求处理，不能把每任务 schema 全部累加或固定成首阶段。
- 分开输入 tokens、已生成输出、下一调用输出预留、窗口硬上限与 0.9 摘要触发线。当前预测比较的是 capacity，却在表格旁标 0.9 触发线；“fits_without_compaction”须解释所用边界。禁止摘要后，触发线是否仍导致提前停止，也须纳入说明。
- 重算 64K/128K/256K 的 floor、假设情景和首次超限点。旧 t6/t9/t12 等阶段结论撤回后按新报告给出，不能只改文案。

验收：至少用两个小的手算样本证明历史持续累积、锚点不重计；真实报告提供逐阶段增量与累计表，不用 fixture 的增长作为上界。

## F2：Letta 容量门使用了错误的真实请求类型

位置：`deployment-assets/letta-no-compaction/ae-no-compaction-letta.patch` 中 `_ae_capacity_gate`。

真实 step 中 `build_request_data` 的结果按字典使用（`.get` 和下标访问），补丁却使用 `request_data.messages` / `.tools`。计数及字节回退都因此失败。

Codex 使用测试提供的 `load_agent_capacity_gate()` 加载方法体，以如下实际形状调用：

```python
{"messages": [{"role": "user", "content": "hello"}], "tools": [], "max_tokens": 2048}
```

得到 `allowed=False, reason=no_trustworthy_count_basis`。当前测试的属性对象替身掩盖了类型错误。

补修要求：使用真实请求结构，继续核 pinned `count_tokens_with_tools` 对 messages/tools 的类型要求及实际计数后端。仅把点号改成下标不算关闭：该函数接收内部 Message 的路径，不能未经验证直接接收 wire dict；也不能把近似计数器称作对应模型 tokenizer。

验收：通过真实 pinned 请求构造函数产生请求，送入真实计数路径；正常请求放行、边界拒绝、缺依据拒绝。精确标注 mock 的依赖，不能再用替身替换关键请求类型或计数接口来证明兼容。

## F3：字节兜底会低估，不能作为安全放行依据

位置：helper `byte_bound_token_estimate` 与驱动 `_capacity_input_counter`。

复现材料：`s = '0123456789' * 1000`，UTF-8 为10000字节，r1 自有 tokenizer 数出10000 tokens，但除以配置2.4只估4167。因此“只会高估”不成立。参数名写 upper bound、解释要求 lower bound，本身也混淆。

补修要求：删除未经证明的比值放行路径；缺可信计数应明确拒绝。若提出替代保守界，必须证明适用于完整渲染请求（含模板、特殊 token 和工具 schema），不能用平均字节/token 或有限样本吻合冒充数学上界。避免另造一套计数实现扩工。

验收：保留上述数字串反例，增加完整请求中的相应边界情景。确保不会因低估让超限请求通过，计数来源记录准确，回退不能标成 pinned/official tokenizer。

## F4：驱动只数本轮增量，且没有证明三角色发送前保护

位置：`ae_cloud_re_multiturn.py::_capacity_input_counter` 与 `MultiTurnBridge._request`。

当前只数本轮提交 Letta 的 `body.messages` JSON，并在 `super()._request` 返回之后判断。它不是 provider 完整上下文，缺少服务端保留历史、system/memory、工具 schema/template，不能作为请求前容量门。

补修要求：

- 核验最终实际 provider 请求输入和本次输出预留，在发送前拒绝；驱动后置诊断可以保留，但不得包装成完整输入容量证明。
- 明确 Agent、用户模拟器、评分器各自真实发送路径。Letta Agent.step 补丁不能自动覆盖直接通过原生 Vita/代理发送的辅助请求。每条路径都要有对应计数与输出预留；辅助角色既有4096不混成Agent的2048。
- 不改变原请求内容来通过检查；中途拒绝保留已完成订单/任务/raw，停止后续阶段并保持完整评分审计边界。

验收：每个角色至少一个完整实际请求结构的离线发送拦截；证明超限时 upstream 发送次数为0，正常请求仍可到达发送替身。覆盖多次串行工具调用后历史增长，不能只放大单个初始 history 证明累计检查有效。

## F5：tokenizer 资产身份与结论不符合原合同

位置：`load_official_tokenizer_assets` 仅查 Qwen3-8B/0.6B 缓存，renderer 是手写复刻；目标为 `Qwen/Qwen3-30B-A3B-Instruct-2507`。

28 条封存请求 usage 近似吻合，只支持这些样本的计数误差观察，不证明目标模型资产/模板一致，也不能把差异原因直接归于工具 JSON 序列化而不做隔离验证。

补修要求：使用目标模型对应官方 tokenizer/template，或提供可核资产一致性证据；不要用其他模型缓存冒称已经完成。优先盘点现有资源。缺资产或官方运行库时列出具体缺项、来源、预期下载体积、受影响验收项，不能下权重或默默扩大联网范围。

无需重新发送封存请求。对同一请求保留本地/provider差额，区分估算校准与精确/保守容量保证。

## F6：加载与审计接线不能仅留作“现场 receipt”

原任务要求离线证明服务启动→opt-in→加载证据→driver→audit兼容。r1 未改 bootstrap/启动器，新manifest并未证明被现有校验链接受。请核定新补丁改变 multicall 已固定文件字节之后，原 manifest/receipt 校验是否仍兼容；若不兼容，在最小必要范围内接入新合同，不放宽旧身份门。

另有已复现问题：`capacity_gate_decision(agent_tags=['ae-no-compaction:999'], ...)` 返回 inactive/allowed，而非拒绝；`_ae_capacity_gate` 入口也在 unknown 检查前直接放行。compact 的未知版本拒绝不等于请求前已拒绝。

审计还需核：逐个 agent 的策略标签必须存在，不能把两臂标签合并为集合后只证明其中一个有；完成结果不得接受空/缺失的 capacity_checks；每条记录须与实际请求及计数证据关联，不能只比窗口和预留字段。

验收：缺/错补丁摘要、缺/错策略、缺计数记录均不能通过；新候选离线启动校验链可消费正确的新证据。现场进程加载仍留作之后部署验收，不在本轮执行。

## 报告文字与测试交付

- 两条查询回包25120占61554约40.8%；39582约64.3%是全部工具回包，不能写“64%是两条查询”。system/memory当前重复归入同一system文本，分类需说明重叠或改为互斥分解，不能暗示各项可直接相加。
- 4012是65536减61524的硬窗口余量；相对58982触发线已经超出2542，不称剩余摘要余量。
- 修复测试失效临时路径，给出当前机器可执行的完整命令及固定源码摘要，不创建假路径掩盖缺源码。
- 只跑直接受影响定向组与一次必要链回归。新0.3正常链与拒绝链必须实际覆盖；0.2旧链通过不能代替0.3正常链通过。

## 交付格式

新r2目录内提供短README（先容量判断）、修正容量报告、F1–F6关闭/未关闭表、精确before/after及SHA、关键diff、定向测试命令与原始日志、部署依赖和未决项。保留r1，明确本轮真实函数/替身/未验证部分。

不能通过的项如实列为未关闭，不为了全绿降低要求。完成后停止，由用户转交Codex定向复核；本轮无部署或真实RUN授权。
