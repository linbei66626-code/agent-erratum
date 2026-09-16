# 真实t4入口r1：只补实际接线，离线交付后停

先读results/ae-local-t4-codex-review-r1/README.md与service-counts.json。Codex用实际9B服务messages/tools计数：3642和17415（加Agent2048分别5690/19463），当次计数时服务max_model_len8192；该次零模型生成。旧19903撤回为9B测量，但8K不足以容纳此构造查询后场景成立，不能说模型必然首条这样查询。

仅修改scripts/ae_01_local_t4_probe.py、对应测试、README/测量脚本。不改旧ae_multiturn_capacity的target（它仍服务30B）、云profile、原始数据、评分或服务窗口。

1. 删除运行时对30B target计数/模板的依赖。每个Agent及用户模拟请求，先将实际将发出的messages/tools及同一chat_template_kwargs送9B /tokenize聊天输入，add_generation_prompt=true；禁止先用旧模板渲染后发prompt。统一在真实chat发送路径做门，缺计数/模型身份不符/形状不支持则零生成。保存计数请求/响应与对应chat摘要。
2. 读取当前服务同模型max_model_len（tokenize响应或models元数据）并逐项验证身份/有效整数。不要写死8192；不以窗口字段宣称硬容量保证。Agent2048、辅助4096都要预留。preflight清楚区分初始fits与后续未验证，不能宣称它测了完整任务。离线测量需要9B自己资产，否则明示缺失，不复用30B。
3. 用户模拟按原任务要求复用原生用户流程，保持当前公开资料/合法用户知识和角色方向，所有实际POST仍经过同一计数与16次预算门；不要另写简化模拟器冒称原生。若复用有具体障碍，明确说明最小改动，不绕过。去掉空响应→“没有了，谢谢”的虚构回退，length/空/不可解析停止留原始证据；不重复追加同一问题。

定向验证只需当前套件加必要反例：真实默认发送构造的tokenize与chat输入一致（有tools及无tools各一）；动态service window改变门但不自动重启；Agent和user容量不够均零chat；旧30B helper不进入run；用户空/length停止且无伪回话；原有真实工具诊断仍过。不要跑全仓/旧交付全集，不再写无关标记体系。

交付后Codex先核计数，再使用已验证的128K服务做单次t4；此任务不部署、不SSH、不调模型、不扩窗口。提供确切依赖与现场命令。

## 2026-09-15 现场更新（不扩大补修范围）

- Codex已实测单卡9B：32K/128K成功，256K请求900秒超时。当前回环8001服务的`max_model_len=131072`，本次再次GET /v1/models确认身份与窗口；不需要你重启/扩容/重测容量。
- 128K合成测试为131008输入+64预留，实际生成2 token、HTTP200；不等于真实t4能力通过。原始证据：`transfers/ae-qwen35-local-capacity-live-20260915-r1/README.md`。
- 仍只修上文三项接线及对应定向测试，不添加新审计体系。读取服务窗口，不能把8192简单改写成131072常量；Agent2048和用户4096预留各自保留。
- Codex已核查当前工作树仍是未补修r1：旧30B计数、固定8192、self_simulated_user仍在。没有启动真实t4，也不把这些入口问题解释为9B能力不足。
- 用户已经同意“先单卡128K测真实t4”。补修交付后按已有授权核对并单次执行，不再询问选哪条路线。
