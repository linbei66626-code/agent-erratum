# AE-01：云端单 t4 离线输入一致性核对器（2026-09-11）

> 2026-09-12 当前状态：渲染、反例构造、事件门已分项修复，Codex 联合复跑53项全部通过。下方“已知限制与未验收项”和各轮状态保留初稿时间边界，不代表这些已修问题仍存在；测试构造轮的正确计数为28项（27通过、1失败），非下文误记的26项。仍未验证云端真实t4完整链路。部署时测试源码根通过 `AE_LETTA_SOURCE` 指定，需核固定commit与源码SHA；显式错误路径不得按skip计为通过。

本页说明新增的离线核对器：`ae_cloud_input_audit.py`、`scripts/ae_01_cloud_input_audit.py`、
`tests/test_ae_cloud_input_audit.py`。**本轮没有云端真实 t4 raw，没有运行模型／API／服务，没有读密钥**；
它只核对已经落盘的运行目录与已关闭的云 proxy journal。连接探针 PASS 不等于任何科学结论。

## 入口与用法

```bash
cd <项目根>
.venv-vita/bin/python -B scripts/ae_01_cloud_input_audit.py \
  --run-dir <已捕获的 run 目录> \
  --proxy-journal <已关闭的云 proxy journal> \
  --output <新的报告路径>
# 可选：--config <本次能力任务配置>  --dataset <冻结数据集>
```

`--output` 在读任何文件之前以 `O_EXCL` 独占创建，已存在时直接拒绝、绝不覆盖。退出码 0 表示
`transport_capture_checked` 与 `input_audit_passed` 同时为真；否则为有界失败，报告里只有 gate 码，
不回显密钥或正文。

## 覆盖的门（真实实现）

- **传输门**：先调用既有 `ae_cloud_audit.audit_cloud_journal`，不重写、不跳过；未关闭的 journal 直接失败。
- **配置门**：`ae-cloud-capability-0.1`、U000828、task 4、仅 t4；逐项核对 65536／2048／4096／0／seed 300／
  8000／32／3／64／12／26214／180／2 MiB／16 MiB；传输候选上限与 config 一致，`max_requests <= 256`。
- **provenance 门**：`plan` 与 `result` 各自的 `provenance.code_sha256` 必须与磁盘上 10 个生产文件
  （`ae_inputs/ae_adapter/ae_http/ae_probe/ae_task_run/ae_vita/ae_capability/ae_cloud_task/ae_cloud_proxy/`
  `scripts/ae_01_capability_probe.py`）逐一相等；`config_canonical_sha256` 用生产 `canonical_sha256` 重算比对；
  给了 `--config` 时还比对配置原文 SHA 与内容；`project_git_commit`、`live_letta_commit_verified` 不许被抬升。
- **数据集声明**：给了 `--dataset` 才做 bytes 重算比对（`verified_against_declared_dataset`）；没给则明确记
  `recorded_only_not_verified`，**不算通过**，`required_for_input_audit_passed` 恒为 false。
- **创建门**：`agent_created` 事件唯一、其 payload 与 plan 的 agent_payload 除随机 `name` 外完全一致
  （name 校验 `ae-capability-cloud-<12hex>`），HTTP 创建体与事件 payload 一致，空 initial_message_sequence、
  无 base tools／sources、单 `ae_preferences` 块、块内容含 7分糖且不含 5分糖与历史证据。
- **system framing 门（严格等价，不是子串包含）**：声明的 system 必须是实际 system 的**精确前缀**，其后必须是
  固定 Letta 的 `<memory_blocks>` 头、唯一的 `ae_preferences` 块（按 `memory_render` + 固定源码的
  `_render_memory_blocks_standard` 包装逐字节等价）、以及只允许声明动态字段变化的 `<memory_metadata>` 尾
  （`AGENT_ID` 必须等于本次 agent id）。追加任何“忽略偏好”类文字都会命中 `system_prefix_changed` 或
  `memory_block_render_changed`。
- **POST 门**：message POST 只允许 `messages/client_tools/max_steps/include_compaction_messages` 四个字段，
  `max_steps=3`；`client_tools` 与 stage_start 声明的工具集合逐项相等、无 `memory_update`、无第二个记忆后端；
  用户消息只允许 `current_task`／`runtime_user` 且 ref 必须已声明、不得重复、不得出现 `t5/`；tool return 必须
  对应已观察到的模型 tool call，且与执行事件 `client_tool_result` 逐字段一致。
- **模型调用门**：每个 Letta POST 窗口内**恰好一个**云 chat 调用；一个窗口出现多个则 `unsupported_multiple_model_calls_per_post`
  直接 fail-closed（不取第一个）。Agent 输出上限必须是 **2048**（4096 只是辅助角色上限，不能放大 Agent）；
  temperature=0、`parallel_tool_calls=false`、stream=false、n=1、无 seed／chat_template_kwargs；工具 schema 与声明一致；
  **对话历史**用“上一轮已确认 turns + 本 POST 新输入”一次性完整比对，续写用 **cloud 原始 response** 的
  assistant turn，不采信 Letta 返回摘要。
- **归一化门**：只接受现有代理声明的变换（`max_completion_tokens → max_tokens`，至多 1 条 rename），
  其余一律 `unexpected_cloud_change`；`token_ids`／`kv_reuse_verified` 必须保持 null／false。
- **辅助角色门**：`native_calls` 快照必须单调增长；每个辅助调用按消息内容与原始响应体唯一匹配到实际云请求，
  并核对 model／temperature／max_tokens=4096、无 tools、无 seed；不是只数个数。
- **事件关联门**：bridge 的每个 request／response 都必须能在 `letta-http.jsonl` 找到对应记录；
  health 与建 agent 是驱动直连、不属 bridge trace，需显式扣除；end 时不得有未消费的 frame。
- **边界**：`task_success`／`scientific_result` 恒为 null；不检查也不声称 token／KV／cache 一致。

## 已知限制与未验收项

1. **尚无云端真实 t4 raw**：正例是生产 driver／builder 路径 + 脚本化离线 Letta 会话 + 真实 `CloudAuditProxy`
   （opener 为 fixture）生成的**合成 fixture**。fixture 的 system 由核对器同源的固定源码渲染函数构造，
   **没有运行真实 Letta 的 PromptGenerator**；真实 framing 仍需真实捕获验证。
2. **负例尚未全部验收**：`results/ae-cloud-input-audit-20260911-r1/cloud-input-audit-tests.log` 显示
   26 项中正例与 provenance 定向负例通过，其余多项仍失败，失败原因是测试的**变异工具**造成的传输层自失配
   （`transport_capture_check_failed`／`unsupported_tool_wrapper` 等）而不是语义门命中，个别测试本身还有
   fixture 形状错误（`KeyError: 'function'`）。这些用例**未验收**，不构成语义门已生效的证据。
3. `results/ae-cloud-input-audit-20260911-r1/` 之外的旧原始记录未被读取或改写。

## 本轮（framing 对照）状态

本轮只修 `ae_cloud_input_audit.py` 的 `render_memory_blocks` / `check_system_framing` 及相关常量，
新增 `tests/test_ae_cloud_framing.py`，未改其他函数、旧测试、CLI／配置／driver／原 raw／上游源码。

- 旧实现的两个错误已修：`render_memory_blocks` 现在自己产出真实的 `<memory_blocks>` 包装（不再在
  `memory_render` 外再包一层 `ae_preferences`）；声明 system 与 memory、memory 与 metadata 之间使用
  真实 PromptGenerator 的双换行分隔。
- 对照来源为固定源码 `/tmp/ae-letta-QaM3ld/letta-v1`（commit
  `56ba9c25552605eec89de8ed3dc6394b625c1993`）。`.venv-vita` 无法正常导入 `letta`（缺 `cryptography`），
  因此用 `ast.get_source_segment` 原样提取并编译真实函数体：`Memory._render_memory_blocks_standard`、
  `_get_renderable_blocks`、`_display_label`、`PromptGenerator.compile_memory_metadata_block`、
  `safe_format`、`get_system_message_from_compiled_memory` 与 `format_datetime`；不改函数体、不手写替代
  期望字符串，期望 system 全部由这些真实函数渲染产生。
- `pytz` 未安装，`format_datetime` 走真实本地时区分支（`timezone=""`），真实 tz 分支未覆盖。
- 结果：`18` 项全部通过（`Ran 18 tests ... OK`）。`r1`（14 项）保留，最终日志在
  `results/ae-cloud-framing-20260911-r2/framing-tests.log`，最终 SHA 见同目录 receipt。
- 收尾三点（2026-09-11 第二轮）：删除常量区重复的 `SOURCE_FILES`／`REF_PATTERN`／`MESSAGE_POST_PATH`／
  `MEMORY_TOOL_NAME` 块；metadata 尾现在精确限定为 archival 至多一次、tags 至多一次且 archival 在 tags 前
  （新增重复 archival、重复 tags、颠倒顺序三个负例，以及只有 tags 的正例）；`RealLettaRenderer` 已直接
  `functools.partial` 绑定提取出的真实 `_get_renderable_blocks`／`_display_label`，不再用 lambda 代替。
- **这仍然不是整个 auditor 的验收**：auditor 其余部分（含负例变异 helper）本轮未改、仍未验收。

## 测试构造轮（2026-09-11，只改测试）

只改 `tests/test_ae_cloud_input_audit.py`，未改生产检查器 `ae_cloud_input_audit.py`（SHA 保持
`66b7d52170805a260447d164a78894de7c0ce4a73a3933a8d7c32daa15755871`）。

- 正例 fixture 的 system 消息现在由 `tests/test_ae_cloud_framing.py` 已验证的 `RealLettaRenderer`／`_Block`
  实际渲染，删除了原先手抄 metadata 与复用检查器自身 renderer 的做法；正常 fixture 仍 PASS。
- `mutate_cloud_body` 的 `cloud_summary` 现在取**原始 upstream_response bytes 与 http_status** 重算，不再把
  「请求」当「响应」；同时保留原始模型响应不动，并断言变异后
  `audit_cloud_journal.transport_capture_checked=true` 才继续断言输入门。
- 区分独占新建写（`write_json`）与临时 fixture 就地改（`mutate_json`）；`rewrite_records` 默认保留
  `sequence`，只有结构性增删才 `renumber=True`。
- 裸 `client_tools`（本线真实形态）与 OpenAI 包裹 schema 分开；跨记录一致性用 `event_mutations` 同步
  bridge 事件，明确是在测深层输入语义，而不是只改 journal 让一致性门先挡。
- `seed` 明确标为**传输门拒绝**测试（生产代理不支持该字段，不会进入语义门）。
- 时间变异改为整体平移、保持单调，真正测 run window；wrong-scope 负例只断言准确的配置／范围门。
- **已知生产缺口（故意留失败，不在本轮修）**：重复的尾部 `task_complete` 事件当前未被检出，
  `test_duplicate_trailing_event_is_currently_not_detected` 如实失败。
- 结果：本模块 `26` 项中 `25` 通过、`1` 项为上述已知缺口故意失败（`FAILED (failures=1)`）；framing 模块
  `18` 项全通。日志在 `results/ae-cloud-mutations-20260911-r1/`。这仍**不是**整个 auditor 的验收。

## 事件生命周期轮（2026-09-12，最小修复）

按 `ae_capability.py:501-588` 的真实 emit 顺序（`agent_created` → `stage_start` → 零或多个
`simulated_user` → `task_terminated` → `task_complete`）收紧 `native_capture`：当前完成的单 t4 要求
`stage_start`／`task_terminated`／`task_complete` 各唯一、`task` 均为本任务、顺序为
start < terminated < complete，且 `native_calls` 快照必须按该顺序**逐个前缀扩展**（长度不下降），
不再用「最长快照」掩盖回退。

- 新门码：`missing_or_duplicate_task_terminated`、`missing_or_duplicate_task_complete`、
  `unexpected_task_in_<event>`、`task_lifecycle_order_changed`、
  `native_calls_snapshot_rollback_in_<event>`、`native_call_history_changed_in_<event>`。
- 原「重复尾部事件未被检出」的失败测试已改为具体事件门拒绝 + `input_audit_passed=false`；
  另补缺失/重复 complete、缺失/重复 terminated、顺序异常、task 不符、快照回退、同长冲突快照测试。
- 增删换序时 `mutate_lifecycle` 会重排 sequence 并把时间改为严格递增，使失败落在目标门而不是
  日志完整性门。
- 结果：输入模块 `Ran 35 tests ... OK`；framing 模块 `Ran 18 tests ... OK`。日志在
  `results/ae-cloud-events-20260912-r1/`。仍**不是**整个 auditor 的验收。

## 工具 wire 转换轮（2026-09-12）

按固定 Letta 源码修正 `ae_cloud_input_audit.py` 的 OpenAI 历史 wire 比较（仅这些方法，其他
system/framing/事件门/limits 未动）：

- **长度映射只用于历史 wire**：`letta/constants.py:67` `TOOL_CALL_ID_MAX_LEN = 29`，
  `Message.to_openai_dict` 把 assistant `tool_calls[].id` 与 tool `tool_call_id` 截到 29。
  新增 `wire_tool_call_id`：native/审批 id 仍精确比较，历史 wire 用固定 29 前缀；两个不同 native id
  共享前缀时判 `tool_call_id_truncation_collision` 拒绝，不做宽松匹配。
- **工具回包必须是真实包装**：`letta/system.py:150-168` `package_function_response` 产出
  `{"status": "OK"|"Failed", "message": <原回包文本>, "time": <时间>}`；`check_submitted_tool_return`
  现在严格核 keys、`time` 为字符串、`status` 与执行状态对应（error 必须 `Failed`）、`message` 精确等于
  原 `tool_return`。原先“wire 只有 raw 字符串”的注释是错的，已删除；fixture 也改为调用**固定源码抽取的
  真实 `package_function_response`** 生成正例。
- **assistant 空文本转换**：provider `content=""` 在带 `tool_calls` 时经 `to_openai_dict` 变为
  `content=None`（`self.content` 为空 → `text_content=None`）。新增 `assistant_history_matches`，
  **只**允许 `""`↔`None` 且带 tool_calls 这一对；非空文本变化、空白、无 tool_calls 的 `""`↔`None`
  一律拒绝，不做 strip。
- 新增测试：原生函数生成的 success-empty / error / 32→29 长 id 正例；错 id（未截断）、截断碰撞、
  错 status、改 message、raw 字符串冒充包装、多余 key、非空文本变化、空白、无 tool_calls 等反例；
  并只读取真实片段 `transfers/lab-cloud-t4-20260912-r1/...private.jsonl` 的工具 id 与包装字段做核对
  （不读用户资料正文）。日志在 `results/ae-tool-errors-20260912-r1/logs/`。**局部方法通过不等于完整
  输入审计 PASS**，真实 run 仍需完整审计。

## 状态

- 正例（生产路径合成 fixture，含 plan/result provenance）→ `status=VALID`、`transport_capture_checked=true`、
  `input_audit_passed=true`、`task_success=null`、`scientific_result=null`。
- 负例：见未验收项 2，逐条失败原因在原始日志中，未做任何“删门／减消息／伪造 PASS”处理。

依据：固定 Letta `56ba9c25552605eec89de8ed3dc6394b625c1993`（`letta/prompts/prompt_generator.py`、
`letta/schemas/memory.py`、`letta/constants.py`）与 `docs/cloud-task-compat.md`、`docs/cloud-capability-readiness.md`。
