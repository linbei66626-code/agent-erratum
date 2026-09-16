# 小任务：接入已取得的目标 tokenizer，只核封存请求计数

2026-09-14。先读项目AGENTS.md。前一轮launcher工程补修已收尾，本任务仅处理新取得的目标资产，不重开launcher、容量投影或端点探测。

## Codex已完成

从Qwen官方Hugging Face仓库固定revision取回5个小文件，无权重、无模型请求：

模型：Qwen/Qwen3-30B-A3B-Instruct-2507
revision：0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe
资产目录：
`/Users/lrc/.agent-reach/ae-01-target-tokenizer/0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe/`

其中download-manifest.json保存URL/时间/字节数/SHA，comparison.json保存与旧Qwen3-8B缓存对照，template-diff.patch是模板精确diff。实际资产已在本机，无需再下载。

已核：
- tokenizer.json（11422654字节）与8B缓存完全相同，SHA aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4。
- vocab.json相同。
- tokenizer_config.json不同，目标SHA a62ff0a2472a0fa1b8eaabcb57c59b58afa42a22831dc141400b6e0cf2b65ce3；差异字段为chat_template与model_max_length。
- 目标模板SHA 64f85b198065d0fba2a81f37e10ed68161ce2c19a754c7100e67e0ca2ee9c326，删除旧8B模板中的thinking分支。merges.txt字节也不同；不要宣称全部资产相同，tokenizer.json内BPE相同与文本文件字节不同应分开记录。
- 目标tokenizer_config.model_max_length=1010000，模型config.max_position_embeddings=262144；前者不能当作.cn服务容量。

## 唯一任务

让共享count_request_prompt及其服务侧副本实际消费这份目标tokenizer/template，并对同一28条sealed请求离线重计。

当前load_official_tokenizer_assets仍只查8B/0.6B；render_qwen_chat仍是硬编码的8B thinking模板。仅改asset_target标签、仅换tokenizer_config路径不会自动替换renderer。

最小要求：
1. 显式固定资产路径/revision/摘要，真实读取与声明一致；不能用两个相等的模型名称字符串代替文件身份核验，也不能缺目标资产时静默回退8B。
2. 共享入口使用目标官方模板语义；优先执行模板本身。若当前环境缺模板运行库，先检查已有可用运行时，确需依赖时列最小缺项，不安装大型模型框架或另起手写通用模板引擎。
3. 工具schema内容纳入计数缓存key，不能只按tools数量缓存；同messages、同工具数但schema内容变长时必须重新计数。这是本计数入口内的最小必要检查，不扩成新模块。
4. 离线读取原28条请求，比较生产共享计数与各自provider usage；generation prompt、工具模板口径明确，记录真实残差，不重发、不用固定±128阈值强行宣称成功、不把有限样本校准冒充未来容量上界。
5. 只跑计数/模板直接相关用例与一次服务副本一致性检查，不跑17套件，不重算t4..t12容量表，不改旧交付或RUN授权门。

## 不在本轮做

不联网、不SSH、不部署、不调用模型、不改实验输入/预算；不探测.cn极限，不把endpoint verification改成verified。官方原生262144与国际站约262K已有文档支持，.cn实际端点容量仍未核。

## 交付

新独占 `results/ae-target-tokenizer-reconciliation-r1/`：短README、资产身份与实际加载证据、28条对照表（不公开消息正文）、最小diff和定向日志。先回答目标模板是否真正被使用、误差如何变化。完成后停止。
