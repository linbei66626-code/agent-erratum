# 只修审计对历史整理多轮 continuation 的错误判定

现场新重审已实际执行（不是fixture）：input-audit-sealed-reaudit-r1.json。身份/stack/预算/容量等门通过，前两个wire帧通过。第3个POST报client_tools_differ_from_declared。
证据在transfers/ae-deepseek-re-complete-20260916-r4/tool-mismatch-diagnostic.json与同目录新报告，均为本次现场只读取得。

精确定位：ae_cloud_re_multiturn_input_audit.py wire_gate：
`if profile == "continuation": profile = "history" if index == 1 else "task"`
实际rewrite t4、index=2、request_id=d6d9fc0e8874420090c6d2a2b49d7ccc，actual_tools只有memory_update，但审计推断task并要求20个工具。连续memory_update使历史整理超过一个工具续轮；不能用POST序号代替阶段。

最小修复：按每个arm/task经过核验的显式history/current_task信封维护阶段；continuation继承其已核实前序阶段，不从被检查的actual_tools反推阶段，不只信任日志标签。工具回包还须匹配前序pending调用。current_task出现才切换到task；新任务history信封按真实协议更新。保留所有工具schema精确校验，不混臂、不将后续任务阶段倒退为history。不更改运行原始字节、plan、result、模型、驱动、服务或评分。

验收仅：连续至少2次memory_update的history续轮通过；显式current_task后任务工具正确；history提前给任务工具拒；task错误退回仅memory工具拒；两臂/跨任务阶段不串。用已有真实封存前3POST作区分性证据，不仅手写fixture。

沿用已实现sealed_audit_copy入口与旧副本7d631dfb…；新审计身份继续实算。定向测试后交付，现场另存新报告继续完整离线审计；若下一个独立错误出现只报告。禁止联网/模型重跑/全仓测试/顺手改其它问题。只改审计模块和相关测试，无须重启服务。
