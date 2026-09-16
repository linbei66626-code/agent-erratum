# DeepSeek：OpenRouter 最小接入

用户已充值 5 美元，授权继续接入。此次只完成离线代码与测试，停止后交 Codex 部署和小额验证；不调用模型、不 SSH、不启动正式 R/E。不要重做容量研究或扩大测试范围。

## 当前事实

- 2026-09-15 Codex 从实验室服务器无鉴权访问 https://openrouter.ai/api/v1/models 得到 HTTP 200；只证明连通。
- 服务器尚无 OpenRouter 密钥，用户另行录入；不得读取/复制 SiliconFlow key。
- CloudConfig.validate、代理真实发送 URL 和传输审计固定 SiliconFlow origin；多轮驱动及输入审计固定模型/profile。因此改配置不足以接入。
- 已有 no-compaction、计数、stack 回执及原始实验材料保持原规则。

## 目标与范围

新增显式 opt-in OpenRouter transport profile，端点固定 https://openrouter.ai/api/v1（注意真实发送路径 /api/v1/chat/completions），模型固定 qwen/qwen3-30b-a3b-instruct-2507。保留现有内部模型别名或显式映射均可，但必须记录映射；不要直接替换旧全局常量。

候选固定 Nebius 路由；实施前读官方 provider-routing 文档和实时 endpoints 元数据确定 slug。要求只允许这一供应商、关闭 fallback、要求参数支持。不要默认路由、自动切模型或为了成功丢弃工具/采样字段。若真实请求参数与供应商能力不符，明确报告待验证项，不擅改实验参数。

1. 代理：显式配置 origin/profile/model/provider；规范化时加入并记录固定路由参数，拒绝客户端覆盖；真实发送与 journal 使用同一配置。所有角色走同一出口。不得把任意 origin 变成白名单。
2. 审计：支持新 profile，独立重算规范化后的请求，核真实 origin/path/model/路由以及响应 provider 身份；身份缺失或错配不能通过正式运行审计。保留 provider usage 和响应身份原始证据。模型别名差异必须显式核验。
3. 驱动/config：接通新 profile；新建候选配置，不继承 SiliconFlow 的端点容量验证状态。目标 tokenizer 可复用，但 OpenRouter 容量必须未验证，正式 RUN 在测量前继续拒绝。保留 65 秒间隔和全部输出预留、工具上限、任务定义及失败停止策略。
4. 提供最小部署说明与有界探针命令：先账号/路由核验，再少量工具调用及长请求测量；不在本轮实际执行。不得通过伪造 capacity verified 绕过门。若需要探针脚本，复用已有容量探针并仅参数化必要传输差异。

优先修改 ae_cloud_proxy.py、ae_cloud_audit.py、ae_cloud_re_multiturn.py、ae_cloud_input_audit.py、ae_cloud_re_multiturn_input_audit.py 和对应 CLI/config；只修改这条路径确实依赖的文件。不要动 Letta stack、tokenizer、记忆算法或原始数据；不是建设通用多供应商框架。

## 定向验收与停止

- 离线真实发送构造捕获：正确 URL、模型和唯一 provider 路由；无网络。
- 客户端覆盖路由、错误响应 provider、缺失身份、其他 origin/profile 均拒绝；旧 SiliconFlow 路径仍过。
- 新 profile 正常记录与独立审计一致，篡改路由/模型/源地址会失败。
- 容量门仍位于真实发送前；未测容量不能启动正式 RUN；原始工具和消息不改。
- 跑新增定向测试及直接受影响的一次回归；不跑全仓、不反复跑历史大套件。
- 独立 results/ae-openrouter-integration-r1/：README、diff、测试日志、部署文件清单/摘要。旧交付不改。完成后停止，列出在线待验证项，不声称已接通真实模型。

官方参考：
- https://openrouter.ai/docs/guides/routing/provider-selection
- https://openrouter.ai/api/v1/models/qwen/qwen3-30b-a3b-instruct-2507/endpoints
- https://openrouter.ai/docs/api_reference/limits

这是新供应商适配，不是旧运行的 bug 补修；正式 R/E 必须使用同一已验证后端从头运行。
