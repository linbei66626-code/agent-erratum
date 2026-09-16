# 给 DeepSeek：单 t4 云端 R/E r1 最小补修

项目：`/Users/lrc/Documents/研究生资料/实验/agent-erratum`。先读项目 AGENTS.md。r1 的交付清单全部字节相符，Codex 禁网复跑 28 项全过，但额外探针发现以下三个确定问题；详细 raw、复现脚本和报告在 `results/ae-cloud-re-pair-20260912-r1/codex-review-r1/`。

本次只补这三个问题和命令说明，交付到新的 `results/ae-cloud-re-pair-20260912-r2/`。允许改 `ae_cloud_re_pair.py`、`ae_cloud_re_input_audit.py`、`tests/test_ae_cloud_re_pair.py` 及新交付说明；保留改前字节与 patch。不要改既有 capability/辅助 checker、捕获端、MemoryPolicy、TaskBridge、proxy、CLI、模型/配置/预算/模板、上游源码、旧 raw/报告/测试日志。不 SSH/部署、不启动服务、不调用模型、不重跑 r4。若发现必须越出文件范围才能修复，先给出具体原因和最小 diff 建议。

## 1. 补齐原生用户回复到 Agent wire 的逐次来源核对

已复现：原生用户实际回复和 simulated_user 事件均为“请继续处理订单。”，但 bridge 向 Agent 发“请继续处理订单，这次明确要7分糖。”，审计仍 VALID。见 `runtime-user-injection/`；正常无注入 control 为 `runtime-user-control/`。

- 核本臂、本任务、按顺序的一次原生用户响应 → simulated_user 事件 → 实际 runtime_user 内容/引用 → POST/wire；来源应从固定 Vita 实际用户回复提取行为确定，不以事件自报正文或 result.transcript 自证。
- 区分会送达 Agent 的非停止回复与只结束对话的停止回复；保留固定 Vita 对停止标记/内容的真实处理方式。
- 对改正文（含原生与事件一起改、但实际捕获没改）、额外/缺失/重复回复、跨臂错配和乱序给有界拒绝码。真实用户确实澄清糖度应通过，不能禁止用户说 7 分糖。
- 正例覆盖无继续回复、一次继续、多次继续、两臂出现相同正文；通过逐次一一对应处理，不能要求所有回复文本全局唯一。

## 2. 评分异常应停止整对，保留已完成证据

已复现：R `finish()` 在请求评分前抛错，R judge 记录失败，但 E 仍开始，driver 报整对完成，审计也 VALID。见 `r-judge-failure/`。

- 按既定故障即停语义结束后续执行，记录 stopped_after_arm/有界原因，清理当前环境并保留订单、transcript、更新与已发生的原生捕获；不重试，不丢已完成 R 证据。
- 审计不能把缺失/失败的必需评分链当完整配对。评分成功但低分、未更新、错误更新和合法工具后的任务失败仍是有效行为；不按评分结果筛样本。
- 覆盖 R 与 E 的评分异常，至少包含请求前异常及已有部分原生捕获后的异常；R 失败不得出现 E stage_start/模型请求，E 失败保留 R 完成证据。覆盖伪造 completed 状态不能绕过审计。

## 3. 同值重复更新按实际执行关联，不按块值全局唯一

已复现：两次连续合法 `replace p007=奶茶偏好7分糖`，真实 MemoryPolicy/TaskBridge/driver 都完成，审计在 `patch_request_missing_bridge_trace` 拒绝。见 `same-fact-replace-twice/`。除此之外代码中 `memory_gate` 按整个块值要求唯一 PATCH，也不适用于同值重复操作。

- 对本臂每次 memory_update 调用、执行回包、PATCH 请求/响应、bridge trace 及后续 wire 按顺序/调用关联逐个消费。保留工具参数/回包/块值、先 PATCH 后正常回包、后续 system 采用实际块的完整检查。
- 不以取第一个、放宽时间窗、去重文本或跳过相同更新来规避；快速 fixture 和真实间隔较大的请求都应正确匹配。同值重复 replace、无更新、错误更新、原来的 p007→p008 double 均应通过。
- 多余/缺失 PATCH、缺失确认响应、错序、响应或参数被改、E 被 PATCH 仍拒绝。

## 交付与命令说明

新增真实路径反例，模型回复继续明确标为 fixture；禁网运行定向与适当全量回归，报告项数/skip/前提。保留 r1 及其 Codex 探针所有字节。交付新 SHA、补丁、验收日志、上述案例的新审计/故障结果；沿用数据集声明路径边界，不伪造容器路径。

修正交付命令的 Vita 源码路径：记录中的实验室源码 checkout 是 `/root/agent-erratum/vendor/vita/source`，数据集是 `/root/agent-erratum/vendor/vita/tasks-full-a4553e1.json`。PLAN/PREFLIGHT/RUN 各用独立的新输出目录，不能将 PLAN 占用的 `<run>` 再交给独占 RUN。只写命令，不执行 RUN/SSH。本轮无需实现多轮 t4→t12，也不收紧 requestor 取值级校验。
