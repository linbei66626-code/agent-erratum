# 单 t4 多工具调用完整串行兼容：离线实现任务

2026-09-12。用户已认可完整保留、按返回顺序串行执行、解决 ID 截短碰撞并离线验收的方案。本文件供用户人工转交 DeepSeek；尚未派发、实现、部署或启动新 RUN。按项目 AGENTS.md，由 DeepSeek 编码和测试，Codex 核验。

## 目标与已定位问题

真实 R/E r2 的 E 首次历史响应返回四个 memory_update，固定 Letta 在 parallel_tool_calls=false 时只留下第一个；下一次 Agent wire 也只有第一个。本次审计正确报 tool_call_count_changed，旧运行保持 INVALID。

输入证据在 `transfers/lab-cloud-re-pair-run-20260912-r2/`：先读 README.md、diagnosis/tool-call-truncation.json、diagnosis/pinned-letta-source.json。proxy journal 第109条请求为 false，第110条响应含四个调用，第115条后续请求只剩一个。四个原始 ID 不同但前29位相同。这些编号指 journal 记录序号，不假定等于零基索引。

目标是兼容同一响应多个已声明的客户端工具调用；不只针对四个特定记忆值。两臂同规则，任何阶段都不默默丢调用。历史阶段仍只开放 memory_update；任务阶段仍用原生工具及 memory_update。

## 已核代码入口

- 固定 Letta HEAD：56ba9c25552605eec89de8ed3dc6394b625c1993。本地候选只读源码 `/tmp/ae-letta-QaM3ld/letta-v1`，必须核字节，不依赖目录名证明版本。
- `letta/agents/letta_agent_v3.py:1335-1342` 是截断分支；SHA `1f11745d6ae86e64e90c76e287d25552a6c7823a8109b178da6951b21c788166`。`_handle_ai_response` 已有客户端批量 approval 返回路径；不要绕到服务器并行执行工具的路径。
- `letta/schemas/message.py` 的 to_openai_dict 同时截短 assistant ID 和 tool_return ID；SHA `c50de8d2792645a51f34f8f264c85bcea8ad252527270e96ebbe23bdd8a8e7e6`。核所有实际序列化入口，不只改一端。
- `ae_adapter.py` 的 LettaBridge.exchange 已对批量 ID 做检查，以列表顺序调用 _execute，再提交整批 returns。优先复用，不重造执行器。其 SHA `f149e83045bced9b484251734fd6ef1c308f50414379acb879931f30f70ce773`。
- `ae_task_run.py` 的 TraceList 已遍历 approval.tool_calls；需实测其原生评分轨迹保留完整批次。
- `ae_cloud_input_audit.py` 的 wire_tool_call_id 是固定旧版29位投影；新协议不能沿用该截短假设，也不能取消数量、顺序、参数和结果核对。

## 修改范围

本轮仅本地离线实现。既有文件只允许按必要性修改 `ae_adapter.py`、`ae_cloud_re_pair.py`、`ae_cloud_re_input_audit.py`、`tests/test_ae_cloud_re_pair.py` 及对应 RE 的两个 scripts/ae_01_cloud_re_*.py 入口。共用桥接改动必须显式 opt-in，旧默认行为保持兼容。

允许新增一个专用兼容模块、一组定向测试、版本化兼容清单及补丁、候选配置副本、一个离线准备/验证脚本和交付目录。命名自定但全部列入交付清单。固定 Letta 原 checkout 不改；在独立临时副本上验证补丁。上游补丁范围限 `letta/agents/letta_agent_v3.py` 与 `letta/schemas/message.py` 的本次调用完整性路径，启用条件明确。不全局替换 TOOL_CALL_ID_MAX_LEN，不升级框架。若发现必须改范围外文件，先交付已查明的具体依赖与最小 diff 建议，不擅自扩大。

不改 capability checker `ae_cloud_input_audit.py`、辅助投影与其测试、Vita、任务内容、原配置、评分规则、提示词、模型、预算和 pacing；不改已封存 results/transfers 文件。可以只读提取 fixture，禁止覆盖旧 raw 或把旧结果改为 VALID。

## 执行协议

1. 原始 provider 回复完整落盘，四条必须仍是一条 assistant 响应中的四个调用。不得代理拆成四次假模型回复、删除调用、改参数、筛选“正确更新”或只保留奶茶。
2. 出站 parallel_tool_calls=false 保持。本次新增的是明确版本化的接收兼容策略：供应商仍返回多调用时，全部交给已声明客户端工具的顺序执行路径；不把 false 偷改为 true，不引入并发。该策略是新的兼容协议，不冒称与旧 Letta 行为相同。
3. ID 优先端到端保留原值，消除本配置路径中的截短；若真实依赖限制使其不可行，才采用确定性、可复算的一一映射。映射必须贯穿 assistant、approval、执行事件、工具回包和后续 wire，记录原 ID；不得按内容猜配、允许重复 ID、或宣称哈希绝无碰撞。实际冲突应在执行前拒绝。
4. 批次在任何工具副作用前核完整结构、ID 唯一性和执行能力。不支持的混合服务端工具/流式不完整批次等明确停止，不先执行第一项。保护模式也须可离线测试：未启用完整兼容时多调用即停，不能仍静默截断。
5. 合法批次严格按原顺序：R 每次等 PATCH 确认后再执行下一项；E 不 PATCH，逐项生成修正。后一项处理基于前一项已提交的状态。同值重复更新、不更新、错误更新不因结果“不好”被排除。
6. 全部调用完成后，每条结果恰好一次、按对应顺序进入实际下一次模型请求；批次中间不额外插入模型请求。核 Letta 内部审批恢复、持久化、去重和 tool_returns 渲染，不能只验客户端列表。
7. 保持原有失败分类：模型无效提议/已有可恢复只读错误仍可产生真实 error 回包；接入异常、无法确定写操作是否完成、PATCH 确认失败等立即停止整对，无重试。保留已执行前缀及其回包、失败处与未执行后缀，不伪造回滚或成功，不建下一臂阶段。

## 审计及版本记录

新 RE 兼容 profile 必须显式声明，R/E 一致，并核启用版本、固定基线 SHA、补丁 SHA、实际改后文件 SHA。离线材料不能冒称服务器已加载或 live commit 已核；提供未来部署时核实际加载文件/启用状态的准确方法，未核则不声称完整部署验收。新增文件纳入 provenance，不按名跳过门。

旧协议仍用旧投影。新协议在 RE checker 中单独实现所需 ID 投影/清单核对；这是精确解释已声明转换，不是放宽现有门。保留逐次数量/顺序/ID/工具名/参数/正文/响应/阶段/来源/未消费关闭、R PATCH 与 E 追加、评分异常停止等检查。未知 profile、缺失清单、源码或映射篡改必须拒绝。

## 离线验收

用真实固定 Letta 经补丁的解析、approval 转换、恢复和 OpenAI 序列化路径，连接真实 MemoryPolicy/TaskBridge/CheckedTaskTransport/CloudAuditProxy；只把供应商、用户、评分应答和外部 I/O 替换为明确标注 fixture。禁止只用新实现生成期待值自证。不得启动网络服务、SSH、联网下载或调用模型；缺运行依赖如实列明，未走到的路径不宣称验收。

- 重现原始四调用响应及相同29位前缀 ID：R 四个成功更新对应四次 PATCH；E 四条修正、零 PATCH；后续实际 wire 四调用及四结果完整。保留本轮真实调用的参数字节，不只手写四个糖度示例。
- 同一记忆连续不同值更新、同值重复更新；检查中间状态、顺序和确认，而不只最终值。
- 普通原生客户端工具批次以及 memory_update 与已声明客户端工具混合批次顺序正确；不得进入 Letta 服务端 asyncio.gather 路径。
- 负例：删除调用/回包，互换不同调用/回包，修改参数或正文，重复原 ID、跨批次重复 ID、映射冲突或篡改、未知 profile、缺/错版本清单，一律有界拒绝。
- 前置保护：原始四调用在不支持模式下拒绝，零工具副作用、零后续模型请求。
- 中途注入不可恢复故障：只执行已完成前缀，后缀与下一臂不执行；保留真实回包，不能由后来伪造 completed 绕过。另测正常 error 回包未被误作基础设施故障。
- 单调用/no-tool 回复仍正常；原生用户逐次来源、真实7分糖澄清、辅助顺序、评分异常等现有 RE 回归通过。

新捕获完整审计需提供真实配置与磁盘 provenance，不跳过门；若数据集声明绝对路径本地缺失，继续如实标明该环境边界，不能伪造路径。旧 r2 用旧固定 checker 仍拒绝，旧证据字节保持；旧捕获不能套新 profile 宣称升级有效。

先跑新增定向与受影响回归；完成实现后全量跑一次。日志列实际测试数、skip、依赖版本；禁网 fixture PASS 只表示离线兼容证据，不表示真实云调用或服务端已经通过。

## 交付

新目录 `results/ae-cloud-multicall-20260912-r1/`，独占创建、不覆盖。提供简短 README、实际修改清单与前后 SHA、项目 diff、上游补丁及基线/改后 SHA、准确复现命令、真实四调用回放和正反例捕获/审计、失败前缀保留证据、测试日志、未决边界。给出候选部署步骤但不执行。

完成上述离线交付后停止。不得部署、启动服务、消费模型预算、自动重跑或发送下一轮任务。
