# 容量预检与禁止摘要 r2：生产接线与计数口径定向补修

日期：2026-09-14

## 目标与边界

r2 经 Codex 复核仍未通过。先读项目 AGENTS.md、原始容量任务书及本页。本轮先打通真实生产入口的离线接线，再统一计数与容量预测；不要重复已关闭问题或全仓回归。

仅本地离线实现、定向测试。禁止 SSH、部署、付费请求、真实 RUN；不改变模型、R/E定义、原始任务顺序、完整历史保留、评分、工具回包上限或输出预算。旧交付/raw/transfers 保持原样，新建独占 `results/ae-multiturn-capacity-no-compaction-r3/`。目标 tokenizer 资产缺失如实保留，不下载权重，不自行扩大联网范围。

## r2 已核进展

Codex 按 README 的环境变量与 `.venv-vita/bin/python -B -m pytest tests/test_ae_no_compaction.py -q` 复跑：36 passed，65.55秒。r2 SHA256SUMS 75项全部一致。这是定向测试与交付内部摘要通过，不是生产链或科学结果通过。

历史相减、锚点误认、dict属性访问、字节比率兜底已有修正；proxy发送前拦截已有实现。保留这些进展，不重写整套方案。以下问题仍开放，不能只修改关闭状态或报告文字。

## R3-1 / F6：必须由真正的服务进程加载禁止摘要补丁

证据：pinned `letta/server/rest_api/routers/v1/agents.py` 导入 `LettaAgentV3`，约2444行由REST服务创建该对象；项目驱动通过HTTP创建/调用Agent，不在驱动进程里执行另一个叠加checkout的Agent。

因此 r2 的“服务使用multicall-only checkout，驱动侧Agent使用叠加checkout”不能落实禁止摘要。`evidence/manifest-layout.json` 仅证明旧manifest接受旧checkout、拒绝新checkout，不能推出新保护链兼容。

要求：

- 让服务实际使用叠加补丁的源码，并以最小新opt-in合同接入manifest、bootstrap/启动器和load receipt验证；不能放宽旧manifest身份检查或宣称被拒绝就是通过。
- 离线证明启动入口选择的checkout与实际导入的Agent、compact、helper源码一致，补丁及tokenizer依赖摘要被校验。
- 正确新证据可被实际校验入口消费；缺/错补丁、策略、源码或加载证据硬失败。保留旧协议旧checkout的原行为。
- 现场进程receipt仍待之后已授权部署验证；本轮用离线导入/启动校验及发送替身核路径，不把真实模型运行作为必要验收手段。

关闭证据必须来自实际入口；不能只AST执行compact函数或只测旧manifest拒绝新树。

## R3-2 / F4：正式CLI不能启用proxy容量门

证据：`scripts/ae_01_cloud_re_multiturn.py` RUN分支（约299–312行）创建 `JSONHTTPTransport`，作为model_transport传给driver。`ae_cloud_re_multiturn.py`约855行只在transport本身或`.proxy`上找`arm_capacity`。`ae_http.py::JSONHTTPTransport`两者均无。

Codex核验：`hasattr(JSONHTTPTransport, 'arm_capacity') == False`，类中没有`self.proxy`。补齐端点证据后，0.3正式入口仍会拒绝接线。fixture所用自带proxy的transport不能代替真实入口证明。

要求：按当前proxy/CLI真实进程关系完成最小配置传递与启用校验；不要给普通HTTP客户端挂一个假函数来冒充已经武装远端proxy。确认Agent、用户模拟器、评分器均经过被启用的同一个实际发送门，角色各自使用2048/4096/4096输出预留。

验收：使用生产CLI入口及实际transport类型，最终upstream替换为离线发送器。正常请求能到发送器，超限时发送次数0，缺/错启用证据拒绝。旧0.1/0.2入口仍保持原合同。不要调用真实端点。

## R3-3 / F2、F5：报告与运行门必须使用同一可信计数口径

证据：`ae_cloud_re_multiturn.py::_official_count_request` 对messages/tools的JSON文本计数，不使用chat template；Letta `_ae_capacity_gate`同样如此。报告逐请求校准使用render_qwen_chat，是另一个算法。

Codex以运行门重数同一封存journal的28条请求：全部比provider更大，本次差额75至3463；序列161 provider prompt=61494，运行门=64957（+3463）。报告±92不能为运行门背书；这些样本高估也不构成对未来输入的数学保证。

要求：

- 报告和运行门共用完整请求计数入口，明确目标模型tokenizer、chat template、generation prompt、tools序列化与特殊token处理；不要把HTTP JSON字节的tokenization当作provider模型输入。
- 对同一封存请求离线对照生产计数函数与provider usage，报告实际误差，不重发请求。不从有限残差直接断言原因或安全上界。
- 目标30B-A3B-Instruct-2507资产仍缺时，保留诊断与正式RUN的区别。Qwen3家族缓存可用于明确标注的探索估算，不能自动标成目标官方计数并放行正式RUN；缺可信依据应拒绝。
- 明确原native模板是否会加token及输出预留如何使用；不要为追求零差额改原请求。

验收：生产门调用的计数函数直接参与封存请求对照；缺目标资产/未核身份的正式路径拒绝，不能只靠端点unverified恰好提前挡住其他缺项。

## R3-4 / F3：近似计数仍可能单独放行

证据：r2 Letta补丁 `_ae_capacity_gate`：

```python
if isinstance(pinned, int) and pinned > (counted or 0):
    counted, source = pinned, 'pinned_token_counter'
```

当官方counter缺失，counted=None，任何正的pinned近似计数都会变成放行依据。与“仅作为更大者第二票、缺可信计数即拒绝”的说明冲突。只删除字节比率函数不够。

要求：可信主计数缺失/失败/身份未核时直接拒绝；近似计数最多在可信主计数已经成功时加严，不能填补主计数缺失。

验收：主计数None或异常、近似计数返回很小正整数的反例必须拒绝；主计数有效时正常边界仍通过。使用真实方法体，别mock掉这段选择逻辑。

## R3-5 / F1：schema仍被逐阶段永久累计，动态增长标签失真

证据：`ae_multiturn_capacity.py::projection_layer`约618–620行：

```python
added = history_tokens_this_task + tool_schema_tokens + task_envelope_tokens + growth
total += added
```

schema是当前请求携带的工具集合，不是每个阶段永久追加的聊天历史。r2虽逐域测量schema，却仍把所有阶段schema累加，导致154861不能作为已核下界。锚点已有24个wire schemas，之后的native19/23集合还需核清记忆工具、包装等真实wire差异。

另：`t4_task_phase_growth_tokens`赋值为`t4_task_returns`（39582），这是工具回包量，不是包含assistant、用户澄清和记忆更新的整段动态增长。

要求：

- 将持久消息累计与当前请求schema/template开销分开。历史只增不减；schema按真实阶段集合替换计入，不能累计历史schema，也不能误删历史中的工具调用/回包。
- 从真实锚点分离已有schema开销后构造后续请求；明确各项来源、不可精确部分与边界。完整任务信封使用实际构造，不用instruction+policy粗和冒称完整。
- 39582若继续用于情景，应改名“按t4工具回包量重复”的假设；若声称实测整段动态增长，需按真实阶段请求差分及材料组成核出，避免把t5材料混入。
- 重算所有容量、首次超限点与输出预留。区分硬窗口和0.9触发停止：compact入口拒绝仍可能在0.9处停止，不能写成只有硬窗口门决定停止。
- README还有算术文案错误：4042=65536−61494，不是65536−61524；后者4012。prompt超58982为2512，total超为2542，分别标清。

验收：手算样本必须覆盖持久历史累计、当前schema替换、锚点t5不重复注入、输出预留及触发线。真实报告给分项轨迹，旧154861及阶段结论保留为r2错误快照，不继续引用为已核结论。

## R3-6 / F6：容量审计仍缺逐请求对应证明

r2 `capacity_gate`验证非空checks、字段、角色与算术，但未逐条匹配真实journal请求ID/body摘要，也未证明每个请求恰有一个check。注释声称“one per provider request”不等于实现已验证。缺失一条、重复一条或用其他请求的计数替换，不应只因列表非空而通过。

要求：将检查结果关联实际规范化请求（ID、角色、输入摘要及计数来源），拒绝缺失、重复、错配；拒绝请求也保留足够审计依据，不公开私有消息。保留原始错误与已完成证据，不把其他失败一律包装为capacity stop。

验收：对正常fixture做上述三类最小变异，审计必须拒绝；正确记录通过。沿用现有journal/receipt合同，必要扩展显式opt-in，不改旧raw或放松原审计。

## 最小交付与停止

r3短README先给修正容量结论，再给R3-1至R3-6状态（已关闭/部分/未关闭）；附精确diff、before/after摘要、生产入口离线测试命令与原始日志、封存请求生产计数对照、新加载依赖及未决项。旧r1/r2不覆盖。

只跑本次直接受影响测试和一次必要链回归，不重复全仓。优先证明真实CLI、服务加载与proxy发送路径，避免用大量fixture数量代替接线。无法闭合时明确缺项，不伪造现场验证、不扩大权限。交付后停止，由用户转交Codex复核。
