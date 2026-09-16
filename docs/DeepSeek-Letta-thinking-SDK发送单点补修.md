# thinking 的 SDK 发送单点补修

2026-09-16 现场新服务 PID10912，回执 deployment/runs/20260916T001359Z-27b26b/patch-stack-load.json 已核；AE_TRANSPORT 两变量均正确。新 RUN ae-deepseek-re-live-20260916-r2 在 R t4 history 首请求 HTTP500 停止。

真实错误：`AsyncCompletions.create() got an unexpected keyword argument 'thinking'`。

根因：Letta OpenAIClient.request_async 用 `client.chat.completions.create(**request_data)`，顶层 thinking 不是 SDK 的 Python 参数。上轮验收从方法体直接跳到代理，遗漏真实 SDK。代理 journal 只有模型目录 GET200，零生成 POST。当前不是模型能力或端点拒绝。

只修这个路径：保持声明模型/字段及冲突拒绝规则；让必需字段通过 SDK 支持的 extra_body 到最终 HTTP 顶层。容量门必须测合并后的实际发送请求，不能把 Python 的 extra_body 包装当成 wire JSON，也不能新增重复 thinking 或覆盖已有 extra_body 其他内容。不要取消代理校验，不改 R/E、提示、预算或旧 Qwen 行为。

验收必须使用现场版本真实 OpenAI Async 客户端与 Letta request_async 路径，仅替换 HTTP transport 为离线捕获：证明不再 TypeError，最终 HTTP JSON 顶层 thinking.disabled、没有 extra_body 包装，经过真实代理且计数/摘要依据一致。缺声明仍拒，冲突仍拒，旧路径不变。禁止以手写 wire 或直接调用代理代替 SDK 验收。

只跑相关定向测试，不跑全仓、不联网、不部署、不调模型。列清部署增量及是否重建/重启/新回执。新独占交付，不改旧证据。
