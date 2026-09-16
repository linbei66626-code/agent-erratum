# DeepSeek：硅基流动小模型最小筛选入口

当前方向已改变：暂停 OpenRouter 补修/部署。用户授权同一 SiliconFlow Key 测多个小模型，能完成任务再本地部署。Codex负责现场运行，本任务只实现离线入口与必要测试，不联网、不SSH、不调模型。

## 固定本轮范围

- 模型仅 Qwen/Qwen3.5-4B、Qwen/Qwen3.5-9B。2026-09-15 已通过用户现有 key 的 GET /v1/models 确认列出。官方模型卡支持本地部署，原生上下文262144；不把它当云端端点容量实测。
- 同一个短任务逐模型顺序运行，共享65秒发送间隔（包括跨模型边界），不并发、不自动重试。模型任务失败记失败；HTTP429/认证/网络错误停止整批，保留证据。
- 第一轮仅复用 scripts/ae_01_native_order_probe.py 已有真实 Vita create→pay 小案例：同一提示、schema、初态、评分，每模型新建独立环境；每模型最多3请求，每请求输出2048、temperature=0。保留原提示和工具返回，不额外给不同模型提示。
- 这是简化工具链初筛，不是 U000828 t4 完整任务，不是 R/E，更不是通过一次就决定部署。筛过后再另行准备完整t4验证。
- 读取密钥 deployment/private/siliconflow.key，只在现场执行；Key不能写日志。

## 最小实现

新增独立 screening CLI，复用原案例构造、工具执行与最终 native paid-order 验收。不修改旧冻结 profile 的模型白名单，不替换全局 MODEL，不通过 monkeypatch 绕过旧校验；使用明确仅包含上述两个模型的独立筛选 transport/profile。

新模型思考模式须明确：优先非思考；按官方 SiliconFlow 参数说明核实 enable_thinking 等设置的接受方式并记录，不能默默依赖默认；无法核实则列为在线待查，不虚构支持。两个模型使用一致显式模式，保留 reasoning/finish_reason/usage；输出被截断归为未完成，不算能力失败或成功。

CLI默认plan，run显式；记录两个模型清单、病例hash、统一输出/请求预算、总发送数、原始请求/响应/工具回包/最终订单状态、usage与耗时。目录独占，不覆盖旧数据。不可拿字符串“支付成功”作通过依据。

## 验收与交付

仅离线定向：两模型wire正确、案例完全相同但状态隔离、真实工具链验收可用、跨模型节流生效、总预算最多6POST、429不重试且停止、缺凭据或未run零发送、旧profile未放宽。

交付 results/ae-small-model-screen-r1/（README、diff、定向日志、可直接使用的plan/run命令、要同步的文件摘要），不跑全仓。实现后停止。生产编码遵循AGENTS的DeepSeek分工，Codex随后核对并执行本轮已授权的小规模筛选。
