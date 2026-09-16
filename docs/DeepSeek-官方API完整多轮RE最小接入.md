# DeepSeek 任务：官方 API 完整多轮 R/E 最小接入

## 目标与交付边界

把现有 t4→t12 的 rewrite / erratum 两臂流程接到 DeepSeek 官方 API，采用同一套已验证的接口澄清；串行运行，取消固定65秒等待。复用现有流程，不把单t4循环包装成“完整R/E”。本轮仅离线实现、必要定向验证与现场清单；不联网、不SSH、不部署、不读真实密钥、不调用模型、不启动实验。完成交付即停止。

先读：AGENTS.md；`transfers/ae-clarified-live-20260915-r1/README.md`；`results/ae-t4-interface-clarified-r1/README.md`；现有多轮配置和驱动。单t4对照已真实通过，但没有证明累积历史/R/E效果。此次是新模型、新接口条件的探索性多轮配对，不能与旧Qwen结果直接合并为正式证据。

## 1. 复用主流程，显式新增候选

保留现有 `ae_cloud_re_multiturn.py` / `scripts/ae_01_cloud_re_multiturn.py` 的真实Letta Agent、原生工具/用户模拟/评分、t4..t12历史输入与逐任务状态流转。保持rewrite改写与erratum保留旧值追加修正的真实差别，其他输入纪律一致。不得注入单t4探针的“正确当前状态快照”覆盖两臂记忆，那会消掉研究问题。

新增显式DeepSeek transport profile及独立配置，不改旧默认常量、旧SiliconFlow/OpenRouter配置与校验。参考 `configs/ae-01__cloud-t4__deepseek-official-screen.json`：官方origin、wire模型 `deepseek-flash`、`thinking:{type:disabled}`。所有实际模型角色（Agent、用户、评分）走声明的同一出口；不声称别名锁定了权重。审计按真实wire模型核对，不能继续写Qwen。若seed等旧参数该端点支持性未确认，明确列为待现场验证，不静默删改或伪称支持。

候选 `min_interval_seconds=0`：不固定sleep，顺序收到响应并完成工具后继续；不并发、不自动重试。保留已有超时、逐阶段/总请求预算、失败即停且留证。不得因变快而顺手放大预算。429如实停止并记录，不声称无供应商限流。

## 2. 两臂一致的接口澄清

复用 `t4_case_variants.py` 的通用语义；沿实际多轮请求构造路径应用，而非只在plan加标签：

- 每阶段从该阶段真实native环境读取完整时钟，与工具校验同源，不硬编码t4的日期/14:30，不取宿主墙钟；缺来源拒绝。
- delivery域实际存在create_delivery_order.attributes时，只追加“目录规格不会自动写入订单”的已核通用说明；深拷贝schema，不改required/执行默认值，不代填参数。其他任务域没有该工具时如实记not_applicable，不强加delivery schema或阻断正常跨域任务。
- 两臂都用同样规则；不改用户人格、任务、正确答案、oracle、工具回包上限26214或原生执行。不能把有记忆修改的完整Agent套进单t4“禁止修改偏好”的system。

## 3. 容量契约必须诚实，不让旧Qwen门误管DeepSeek

现有0.3依赖Qwen tokenizer、250K测量、Letta服务计数门，不能换模型名后照搬。新增独立候选协议，不放宽旧0.3，也不将旧模型计数/bytes÷4/响应usage冒充DeepSeek请求前精确计数。

候选明确：官方上下文声明与现场测量分开；端点token容量未测、无已验证本地计数器、capacity_is_a_guarantee=false；逐请求字节预算只是操作保护（初始沿用旧多轮2097152字节，不改成单t4的262144字节），输出预留仍Agent2048/辅助4096。记录实际请求字节及服务返回usage，失败保留已完成阶段、不运行后续阶段。

逐层检查driver/bridge/proxy/Letta实际发送与compact入口：禁摘要仍生效；新profile不得触发隐藏的Qwen计数、静默压缩或历史裁剪。若要替换服务侧旧容量门，必须走明确的模型专属策略分支并保留请求前字节门，不能关闭所有门。涉及服务补丁时同步现有stack manifest/receipt链并列出真实部署增量，不新造第二套审计框架。

这项科学协议选择尚待Codex核对：允许以“未保证token容量的探索性运行”启动，必须由独立显式选项声明，默认RUN仍拒绝未解决的容量前提；不能伪造verified来让旧门通过。若在本次小范围内无法正确处理服务链，交付明确的阻断位置和最小剩余项，不拿mock通过宣称现场已可启动。

## 4. 最小区分性验收

只跑新增与直接影响套件，不跑全仓或重验全部历史交付。

1. 真实proxy发送构造捕获：官方URL、wire模型、非思考参数正确；各角色经过同一出口；0秒不sleep；旧profile行为不变；非200一次即停。
2. 真实多轮构造：至少t4→t5及一个非delivery阶段，两臂时钟取自各自阶段；仅匹配的工具说明变化；R改写/E追加差别仍在；没有正确状态快照/评分答案注入。
3. 候选容量与禁摘要：超字节预算在发送前零上游；DeepSeek不调用Qwen计数；摘要入口零summarizer、消息不丢；默认不接受未经声明的探索容量策略；旧0.3拒绝规则不变。
4. 最小离线两臂链到审计：使用现有真实状态转换/输入审计，替换外部依赖，不mock关键被测门。错误模型、未计入的请求改动、缺服务身份、单臂才加澄清等反例必须失败。脚本化模型通过只验证工程，不写模型能力结论。

## 5. 交付（保持简短）

独占 `results/ae-deepseek-re-multiturn-integration-r1/`，已存在即拒绝；旧raw/results/transfers不动。README优先列：可运行/阻断状态、实际修改文件、定向测试、未核项；附关键diff/必要证据/SHA。

先在README用一小段讲清“复用哪些入口、新增哪一层、容量采用什么口径”，然后实现，不要求另等一次批准。若范围明显超出上述最小接入，停止扩张并说明。

给Codex现场清单：需同步和备份的文件、是否重启Letta与需重核的receipt、模型目录/模式参数待核项、校园网无鉴权HTTPS检查、plan/preflight、全新目录的一次t4..t12两臂命令、预算上限和结果取回位置。配置先标候选，未现场核对的参数不要写verified；本轮不执行这些命令，不新增定时任务。
