# 已完成运行：只修/核对审计ID映射，不重跑模型

证据：transfers/ae-deepseek-re-complete-20260916-r4/。两臂18/18完成，337生成200；7原始文件SHA与服务器一致。绝对路径重新审计后INVALID assistant_history_changed。

首条真实provider ID call_00_bIzNFTjB8CduPr3zBpyX3052 → 下一请求历史call_00_bIzNFTjB8CduPr3zBpyX3。文本与参数不变。168个ID前29字符无冲突；全历史7806工具调用引用只发现ID截断及既有空字符串→null转换。诊断不是正式审计通过。

任务：核实封存manifest对应真实Message序列化与preserve_tool_call_id语义，定位为何当前multicall审计要求全文ID，而当前服务发出29字符。只根据真实服务行为建立受协议限定、唯一可逆的原ID→wire ID映射，调用和回包两端都核验；任何冲突、任意替换、参数或非空正文变化继续拒绝。不能全局放宽成startswith或跳过assistant_history_matches；不得修改原始捕获/plan/result/旧报告，不改变模型/任务/补丁。

最小定向测试：本现场首条长ID与回包可对应；同前29字符的两个ID拒绝；未知/错配ID拒绝；非空文本/参数修改拒绝；旧Qwen/旧协议不放宽。然后用真实公开入口在原始封存字节上完整离线审计，报告实际通过的门和下一个阻断点。环境绝对路径差异用既有显式参数与现场重审清单处理，不伪造provenance。

只做此处诊断与审计补修，若后续出现不同根因，报告具体证据后停止，不扩大成整轮开发。禁止联网、模型调用、重跑实验、全仓测试。新独占交付，说明部署审计增量；不需要为审计改动重启Letta。附绝对dataset路径的现场离线重审命令，输出必须新文件。
