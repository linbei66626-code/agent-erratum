# 原始多轮r1：审计闭环与完整离线链补修

## Codex初步核验结论

r1交付SHA清单检查通过，但不能进入部署。以下来自源码静态追踪与交付日志检查，不冒称已执行变异探针或完整独立审计。保留r1及并行回放工作全部字节。

1. `ae_cloud_re_multiturn_input_audit.py:780` 的verify在post_attribution/cloud_calls/native_capture_gate/memory_gate之后直接进入separation/task_scores并判VALID；未执行原核验器的agent_posts、逐帧wire核对、auxiliary及未消费计数闭合。cloud_calls主要解析云记录，不等于把实际云请求与driver预期逐一核对。报告初始空mapping/零unaccounted不能当作检查已执行。
2. native_capture_gate仅核原生回复与simulated_user事件；task_material_gate对runtime_user只核类型/任务并计数，未见逐次将真实回复正文与实际发送正文一一绑定。需要沿完整云输入闭合这条链，而非只核两份内部记录。
3. memory_gate主要比较PATCH与result更新账本、最终E块；必须进一步核实际工具调用、确认回包、后续模型wire中的更新/修正，以及逐任务持续历史，不能只相信账本。
4. 生产是0.2，离线链却仅0.1、t4/t5；不足以验收生产18任务。任务书已明确授权完整离线18任务fixture链，禁止的是真实模型RUN；“未授权”说明应更正。
5. README测试统计自相矛盾（86/91、所谓新增5失败与零回归、fresh-process失败都归缺checkout）；逐个测试ID与日志核实。基线同样失败不能证明相关功能通过。
6. LAB-COMMANDS中的 `/root/agent-erratum/vendor/letta/letta-v1` 与既有现场 `/root/agent-erratum/vendor/letta-v1` 不一致；仅export变量不替代已核MulticallRuntime/bootstrap启动链。现材料没有完整proxy/watchdog/服务清理组合，不能直接执行。

## 授权范围

仅离线补修r1新增多轮模块、CLI、配置、测试和新交付目录；确需改ae_vita.py可做最小兼容补修并保存完整before字节。不改共享已核审计/代理/MemoryPolicy/上游/启动器/watchdog/旧数据/旧交付/回放代码，不联网、不SSH、不部署、不调用模型。不要原地换旧生产文件做并行全仓基线；在隔离副本或新进程中验证。

## 必须完成

- 为多轮实现实际Agent请求/响应、Letta framing、OpenAI wire、工具schema/参数/调用ID/回包、累计消息和内存逐次核对；不得直接调用带单t4假设的方法而不适配。
- 为用户模拟与评分辅助调用实现按臂/任务/阶段窗口和权威捕获顺序的唯一匹配，闭合全部云调用。报告中每个checked/mapping/count都必须来自实际执行的检查。
- 历史准入、当前任务和回复索引均按(arm,task)隔离；不能另一臂已有历史替本臂满足前置。阶段身份不仅从result声明核对，还核实际HTTP agent路径/云framing。
- runtime_user正文逐次绑定真实原生回复及实际wire；评分核真实完整链和本任务transcript/环境，低分继续、评分异常停。
- 使用现有可信归档与已核补丁/嵌入器，在独立目录离线重建真实patched checkout，核基线、补丁与结果SHA。不伪造缺失/tmp目录、不生成假源码、不下载、不放宽manifest；缺必要材料时具体报告。
- 在0.2下跑完整R(t4..t12)→E(t4..t12)的生产CLI/driver+真实桥接/代理+真实Vita环境fixture链；使用生产manifest生成器及真实bootstrap离线receipt，明确不等于服务进程实测。跨任务记忆更新、E修正累积、domain切换必须真正发生，不能9次无动作空跑冒称覆盖。
- 新反例应保持传输链内部一致、仅破坏目标语义，确认由目标门拒绝：云输入遗漏历史或E修正、更新回包变更、runtime_user注入、provider调用遗漏/交换、辅助响应交换、额外未消费云调用、评分缺失/错任务、后续gold重置。完整正例必须通过同一套门，禁止跳provenance或digest stub。
- 修正测试统计，按ID列环境失败与代码失败；恢复必要源码后跑受影响定向组及fresh-process前提，明确0 skip。不要无理由再跑全仓。
- 部署材料给真实CLI对应的启动/代理/独占目录/receipt/watchdog/审计/清理/回收组合；总预算256不扩大，8h仅候选未批准。按真实单t4的33次调用和评分滑窗补充耗时不确定性，不承诺9任务一定在预算内。

交付新目录results/ae-cloud-re-multiturn-original-r2/，含精确diff、before/after完整字节SHA、针对上述静态疑点的可复现探针、完整18任务0.2 fixture证据和短说明。未完成不得标“完整离线验收完成”。完成后停止，不执行真实RUN。
