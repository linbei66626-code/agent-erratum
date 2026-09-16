> **GitHub 评审快照 · 2026-09-16**
> 请先读 [当前评审入口](review/START_HERE.md)。以下原 README 是历史说明，其“最新”不代表当前状态。
> 本仓库为源项目的精选快照，不含密钥、完整运行日志、依赖检出树和完整数据集。

# Agent Erratum · AE-01 最小适配核心

这是 **VitaBench 派生持续对话实验的原型代码**，不是正式 benchmark。主线是 U000828 的 **t4–t5 两组接通调试**：Letta 保留跨任务消息，分别使用 rewrite / erratum 记忆策略，连接 Vita 原生环境、模拟用户和评分器。

**最新补充（2026-09-11）：一次 Qwen3-4B-Instruct-2507 基础能力探针未通过，runner INVALID；原始包现已本地验收。** 新模型以 65536 窗口加载；只给正确当前偏好、不发送历史、不开放记忆更新，仅跑 t4。最大输入 20765 tokens，未撞窗口；但没有创建订单就声称已下单、地址写成住址，最后输出虚构长订单号至 2048 token 上限。实际订单为零、评分器未运行，不作为正式任务分数或 erratum 证据。用户已下载并解压 48 文件，SHA 与逐文件字节均相符；原始记录支持此前诊断，但完整输入审计仍未完成。旧平台最后核查已停止。实验室迁移的最新执行状态与下一步统一见 [实验室迁移入口](docs/lab-migration.md)，不在本段复制运行态。本次不改变主线配置，也没有删除旧 8B。

**此前主线状态（2026-09-11）**：真实任务尝试 r2 已执行，但**整体 INVALID**。两臂均走完 t4（`sub_U000828_4`）对话和原生评分流程；进入 t5 时，rewrite 的新增历史请求为 **31501 input tokens**，被代理容量门阻止，未转发该次推理。当次没有静默截断、扩大窗口或改模型重跑。

当前窗口 32768，代理 input 上限 28672、预留输出 4096；即使只加该请求实际 `max_completion_tokens=2048`，也有 `31501 + 2048 = 33549 > 32768`。输入审计在 `proxy_blocked_rejected_or_incomplete_response` 处失败，**其余完整输入检查尚未完成，不能声称 R/E 语义有效或发布分数**。这次先暴露了容量边界，不是已证明的 erratum 质量退化。

本地与服务器全套 **158 项测试均通过**。测试中的原生组件调用使用 fixture 回包，与上述真实模型运行分开。**本轮证据包已下载、解压并验收：SHA 与服务器记录一致，217 个文件逐一一致，代码／配置及审计输入哈希相符。** [本地任务证据](results/ae-task-wiring-evidence-20260911-r1/)与[验收详情](docs/local-deployment.md)。此前平台记录停机 2026-09-11 10:53:51、GPU 0 MiB；本次仅本地验收，未重新查询平台或启动服务。

历史接通证据：2026-09-11 已部署隔离的 Letta + vLLM/Qwen3-8B，并完成一次真实 `read_probe` 合成工具往返，远端探针输出 PASS。原始包已下载、解压并验收：包 SHA 与服务器记录一致，100 个文件逐一一致。[本地证据目录](results/ae-deployment-evidence-20260911-r1/)；SHA、范围和恢复事项见 [部署记录](docs/local-deployment.md)。该次部署于 2026-09-11 09:05:15 关机；这是历史记录，不代表当前服务状态。

研究状态唯一入口：[Obsidian 当前状态](</Users/lrc/Documents/Obsidian Vault/论文阅读/80-实验与复现/Agent场景多轮修正/Agent场景多轮修正-当前状态.md>)。

## 代码做了什么

| 文件 | 职责 |
|---|---|
| `ae_inputs.py` | 核完整数据 SHA 与用户位置；只用 t3 偏好和资料初始化；投影连续 t4–t12，或接通子集 t4–t5；环境／评分数据另存，后续真值不送入 Agent |
| `ae_adapter.py` | 构造 Agent 创建参数；处理模型提出的 `memory_update`；将待执行客户端工具交给外部环境并续传真实 ID 的回包；同一 Agent 跨任务保留消息 |
| `ae_task_run.py` / `scripts/ae_01_task_run.py` | 限定 t4–t5、R/E 两臂的持续对话驱动；按任务切换原生工具 schema／环境，处理原生用户回复和终止规则，保存部分失败与轨迹；默认 plan，显式 preflight / run |
| `ae_vita.py` | 核固定源码／完整数据；调用真实 Vita 环境、同一 PersonalizationUser 的子任务推进、原生终止谓词和轨迹 Evaluator；私有保存 persona、环境与评分依据，不注入未来任务真值 |
| `scripts/ae_01_preflight.py` | 不加载模型的真实数据检查；保存 Agent 可见资料、历史消息、任务指令预览、字符／字节数和来源哈希；拒绝覆盖已有结果 |
| `ae_http.py` | 标准库 JSON HTTP；请求发出前持久化日志，无自动重试／重定向，默认只连 loopback |
| `ae_model_proxy.py` / `scripts/ae_01_model_proxy.py` | loopback 模型审计代理；原样记录并转发完整模型请求／响应，不修 prompt；按真实 chat template 的分词结果检查窗口与输出预留，保留 token usage 对照 |
| `ae_input_audit.py` / `scripts/ae_01_input_audit.py` | 运行后的独立只读检查：关联客户端轨迹与实际模型请求，核消息／工具回包、记忆渲染、辅助模型输入及 token 记录；输出新报告，不改 raw、不计算偏好正确率 |
| `ae_probe.py` / `scripts/ae_01_connection_probe.py` | 默认只预览计划；显式执行时核配置并进行一次合成工具往返，不执行 Vita 任务 |
| `ae_capability.py` / `scripts/ae_01_capability_probe.py` | 独立的单任务当前状态能力诊断：公开证据支持的 7 分糖快照、无历史／记忆工具、真实 t4 回路；实际订单检查与输入审计／用户确认分开，不能自动判正式 VALID |
| `configs/ae-01__capability__qwen3-4b.local.json` / `scripts/deployment/start_vllm_qwen4b.sh` | 本次独立 4B／65536 窗口配置及启动器，不替换旧 8B 主线配置；模型逐文件对固定 revision 校验 |
| `configs/ae-01__connection-probe.local.json` / `docs/local-deployment.md` | 接通配置示例、已核服务器环境、独立部署边界和待办 |
| `tests/` | unittest；协议 fixture、审计反例和可选真实 Vita 组件集成测试。真实组件测试须 `.venv-vita` 与固定本地源码／数据，否则跳过；所有模型回包均为 fixture |

本原型不修改 Letta 或 Vita 源码。驱动直接构造原生环境与 User／Evaluator，**不经过 Vita 原 PersonalizationOrchestrator 的 Rewrite/RAG memory.update/read 路径**；只有模型调用本项目 `memory_update` 才更新研究记忆，避免双重整理。环境按子任务快照切换、线程注册表串行隔离；两臂各自的 Letta Agent 保留跨任务消息。外层循环、轮次上限、domain policy 的注入位置是派生设计，不冒称原生 orchestrator 或官方 benchmark。

当前接通配置见 `configs/ae-01__task-wiring.local.json`：Agent、模拟用户、评分器三种角色都使用 **Qwen3-8B**，仅用于接线调试；窗口 **32768 tokens**，Agent 输出上限 2048、User／Evaluator 4096，保留模型默认 thinking。窗口是每次输入与输出预算，不是可无限保留历史的保证；超限停止，不静默裁剪。

## 两组边界

共同工具参数为 `operation, fact_id, category, content, evidence_ref`。模型自己决定是否更新、更新何值；代码仅验证操作结构、可见来源、稳定 ID 和显式字符容量，不判断语义正确性、不补正确答案。若模型不调用工具或提交错误偏好，不能由适配器纠正。

- **R / rewrite**：对独立 block 应用模型提交的修改，通过 agent 专用 PATCH 确认后续传成功回包。回包保留实际更新与整块新内容；它也在尾部包含新信息。
- **E / erratum**：不 PATCH 原 block；根据模型调用生成 `[STATE UPDATE]` 回包。进程内有用于解析 ID 和记录操作的逻辑账本，但不向模型提供读取接口，也不重新注入这个最新表。
- E 可以连续更新同一 ID，不要求第二次旧值仍匹配初始 block。两边的逻辑容量限制一致；不静默截断。
- 当前提示词和工具回包见 `SYSTEM`、`MEMORY_TOOL`、`MemoryPolicy.update`，版本 `ae-memory-tools-v0.1-prototype`。这是自定义原型，**不冒称 Models Take Notes 原文模板或原生 Letta baseline**；也不是冻结的正式运行配置。
- 两组的 block 位置／内容、正常回显长度和后续行动可能不同，比较的是更新策略整体，不是单轴 KV 归因。

## 离线复核

核心 fixture 测试仅需 Python 标准库；真实 Vita 组件集成测试另需现有 `.venv-vita` 与固定本地源码／数据。两类测试均不需要模型服务或账户密钥。

```bash
cd /Users/lrc/Documents/研究生资料/实验/agent-erratum
python3 -B -m unittest discover -s tests -v

python3 -B scripts/ae_01_preflight.py \
  --dataset /tmp/ae01-vita-8WHDvk/tasks-full-a4553e1.json \
  --output results/ae-01__offline-preflight__u000828__t4-t12__r2.json
```

`r1` 已生成，复跑必须用新输出路径。上面 t4–t12 命令只做历史范围的静态数据预览，不授权运行这些任务；当前接通范围仍为 `--end-turn 5`，不是两次糖度更新实验。此输入预检的默认 token 为 `null`，不把字符当 token；可另传 `--tokenizer` 指向已有的本地绝对目录，只尝试离线分词。它不代替模型代理对完整 chat template、工具 schema 和运行输出的核验。

2026-09-11 验证：新增 NLTK 启动适配的 12 项测试后，本地与服务器全套 **158 项均通过**，分别 4.030 秒和 10.751 秒。最新本地日志为 `results/ae-01__task-wiring-offline-tests__20260911-r3.log`，服务器为 `deployment-logs/task-tests-20260911-r3.log`；原生无模型预检见 `results/ae-01__native-preflight__t4-t5__20260911-r1/`。其中原生集成测试覆盖 t4 delivery → t5 instore、真实只读搜索／序列化、同一模拟用户推进、原生评分提示词与解析；固定评分回包只验证程序行为，不是 Qwen 输出。

2026-09-10 第一轮核心验证：**43 项单元测试通过**；固定真实数据静态预检通过，9 个任务、327 组历史，历史紧凑 JSON 共 190,919 字符。记录在：

- `results/ae-01__offline-tests__r1.log`
- `results/ae-01__offline-preflight__u000828__t4-t12__r1.json`

预检报告中的 `formatted_agent_messages` 只有历史资料消息和当前指令预览。实际 `run_stage` 还会添加任务 ID、模拟日期、domain policy 和来源标记；policy 放在当前任务的 user 消息中，而非原 runner 的 system 位置，两臂一致。**这不是已抓取的最终模型请求**；真实运行的完整请求由模型代理记录，再交输入审计器核对，旧域规则对模型行为的影响仍不能仅靠结构检查排除。

## 固定来源与运行约束

- VitaBench 2.0：`f60169e89f30499cb7883f3dad76bd03facc908d`，只读副本 `/tmp/ae01-vita-8WHDvk/source`。
- 数据 revision：`a4553e13ec081be65cc37b48845da5819692d438`；完整 SHA-256 `3a05f7e2742f204ae28b71db95b14e2daa94c9ff354f510a7d9c707564c7466e`。输入固定 `$[25].id=U000828`，非任意数据接口。
- Letta archive：`56ba9c25552605eec89de8ed3dc6394b625c1993`，只读副本 `/tmp/ae-letta-QaM3ld/letta-v1`。该旧版已弃用、处于维护模式，活跃开发已迁移（原 README“停止维护”的说法过强）；本轮已安装并仅在服务器 loopback 启动，**不用于公网服务**。`agent_type=letta_v1_agent` 在该版本路由到内部 V3，不能混成 V2 或当前 Letta Code。
- `/tmp` 资料可能被清理；固定版本与哈希是复核依据。当前未初始化本项目 Git；预检保存代码 SHA，不虚构 commit。
- 创建参数关闭默认工具／后台记忆、autoclear 等；GET 显式加载并检查 block、工具、来源、组和 pending approval。只发出配置不等于运行条件已经满足。
- 专用 PATCH 会重编 system；普通 V3 step 不保证重编。GET 的 block 值检查仍不能代替实际模型输入检查。
- AE driver 检测到摘要事件、异常 user 消息、历史 ID 前缀丢失、截断标记或不确定副作用时停止，不自动重试。**这不是所有层零重试**：r2 的代理拒绝后，框架内部又尝试数次并收到 503；代理已 fail-closed，没有继续转发推理。源码中非流式 summary 可显示为 user_message，因此做了额外检查。但 ID 保留不证明内容未变，未见截断标记不证明未截断。
- `exchange` 完成只表示 `end_turn`，`run_stage.task_success` 始终为 `None`。现有驱动使用原生模拟用户和终止谓词后才推进子任务，不能把模型一次答复当任务成功。原生 RewardInfo 保留为调试信息，`scientific_result`／`scientific_success` 保持 `null`。
- `assert_separate_arms` 应在创建并核对两组状态、绑定环境后调用；不同 agent/block/环境实例不能共享。实际环境按子任务快照切换，不声称连续世界状态。
- `LettaBridge.trace`、客户端 HTTP 日志、原生 User／Evaluator 调用快照和模型代理日志职责不同。代理额外保留实际模型请求／响应；完整输入审计是独立后处理，不能用 ID 保留、HTTP 200 或对话结束替代。驱动完成仅为 `WIRING_COMPLETED_AUDIT_PENDING`，不自动赋予 VALID；即使输入审计通过，也没有正式科学结果。进程异常时保留已有记录，AE driver 不自动重试或覆盖；框架内部重试须从实际日志另核。

## 无模型的接通计划预览

```bash
cd /Users/lrc/Documents/研究生资料/实验/agent-erratum
python3 -B scripts/ae_01_connection_probe.py \
  --config configs/ae-01__connection-probe.local.json
```

**不带 `--execute` 不访问服务**。这是保留的合成接通检查，不是 t4–t5 任务驱动。只有服务器 loopback 服务已就绪后，才在已授权窗口显式加 `--execute` 并提供全新的 `--output-dir`；已有探针结果不能覆盖。

探针会让模型使用一个返回随机标记的 `read_probe` 小工具，核对真实调用、回包 ID 和最后回答；合成探针成功只代表这条工具回路接通，`task_success` 为 `null`，不是偏好正确率。失败记录实际错误，既不自动重试也不代填模型答案。创建的测试 agent 留 ID，不自动删掉。

示例 8192 窗口／1024 输出上限只用于短探针，不是本轮 t4–t5 的 32768 窗口配置。来源与部署限制见 [部署记录](docs/local-deployment.md)。

2026-09-10 接通准备自查：全套 **79/79** 通过（原核心 43、HTTP 层 18、探针 18）。其中一项使用两个真实本机 HTTP 测试服务串起完整工具往返，但返回内容仍是预编写 fixture，**不是 Letta 或 Qwen 的输出**。

- 测试记录：`results/ae-01__connection-offline-tests__r1.log`。
- 无联网计划：`results/ae-01__connection-plan__r1/plan.json`，`status=PLAN`、`network_called=false`、`model_called=false`，含 config 与源代码 SHA。它不是一次已执行的 probe result。

## 下一步

8B r2、4B 原始证据包与离线容量诊断已保全验收。当前转实验室，先传入资料、建隔离 Python 3.11 依赖环境，再适配候选 API 并做小探针。不是只改 base_url；不直接用未通过能力探针的配置继续主实验，不因失败自动增加输出额度或改 prompt 重抽成功；原始 INVALID 保留。

原 ¥10 预算改由用户自行管理，不再当作本轮固定 cap；这不扩大任务范围。主线仍限 t4–t5，独立能力探针仅 t4，不自动扩到完整 t4–t12。当前没有正式真实任务正确率、偏好更新质量、KV 命中或加速结论。
