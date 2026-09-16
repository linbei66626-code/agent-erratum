# 原始多轮r2：顺序、评分窗口与部署材料最小补修

本轮仅离线补修，不SSH/联网/部署/调用模型；保留r1/r2、Codex核验目录和回放工作全部字节。允许修改r2新增多轮审计/测试/fixture及必要部署说明；不改共享审计器、上游、记忆策略、watchdog或旧协议。完整before字节与SHA须冻结，新交付写r3独占目录。

## 已执行的Codex有界探针

目录 `results/ae-multiturn-r2-codex-review-20260914/`，probe.py与probe-results.json。
探针仅直接调用真实gate，输入为明确标注合成对象，不是完整捕获变异，不能声称完整审计已被绕过。

1. auxiliary_gate（ae_cloud_re_multiturn_input_audit.py:1355）仍从整个remaining集合按请求和响应内容找matches，要求全局唯一，再remove。native顺序A,B，云顺序B,A时被接受，mapping_order为c2,c1；相同请求+相同响应重复两次则错误拒绝ambiguous。注释中的阶段窗口未落实；也未核代理捕获角色与native角色一致。
2. judge_gate（:1273）硬性len(evaluators)==1，3请求直接judge_call_count_changed。已封存真实单t4的R/E各num_windows=3，见transfers/lab-cloud-re-watchdog-run-20260913-r1/.../result.json。要求单次请求与固定原生滑窗评分不兼容；不能限制或改上游评分以符合fixture。
3. LAB-COMMANDS引用scripts/deployment/ae_network_precheck.py，本仓库未找到该文件；启动部分没有实际完整MulticallRuntime参数命令。组合仅EXIT时kill代理，审计前未关闭/等待代理，无法保证journal已封口；异常时未保证Letta/DB清理。当前命令不可直接用于部署。

## 修复与验收

A. 辅助调用严格按权威捕获顺序逐次配对，核(arm,task,phase)窗口、捕获角色、完整请求/参数/响应；不跳过较早记录寻找未来内容，不要求相同内容全局唯一。按阶段索引消耗，不按内容决定归属。窗口取可核阶段/请求捕获边界，并处理最终评分、停止回复等边界。新增顺序互换拒绝、相同内容重复接受、跨任务/跨臂/窗口外/角色错配拒绝的完整捕获反例。

B. 依据固定Vita真实评分源码核滑窗展开及聚合链，支持每任务多次合法evaluator请求。每个窗口只核其应包含内容，不要求每个窗口包含整条transcript；窗口数量、顺序、每个请求、响应与最终reward聚合须一致。不得仅将==1改成>=1；不得以子串存在替代结构化轨迹核对。真实原函数/原消息类驱动离线多窗口正例；缺/多/重复/错序窗口、错任务轨迹、评分聚合篡改必须拒绝。同值通用话术出现在不同任务不应自动被当串台。

C. 对extra_cloud_call补传输链完整有效且transport_capture_checked=true的反例，最终应由未消费语义门拒绝。现有孤儿记录被transport_capture_check_failed拒绝保留，但不能称它已证明未消费门有效。

D. 修正部署材料：复用已核现场外层脚本与letta_local的实际CLI/MulticallRuntime接线，核所有文件和参数存在；无凭据网络检查可内联curl，不凭空引用脚本。网络失败立即停；代理就绪后RUN；成功/失败/信号退出都清理本次进程，审计前关闭并wait代理使journal封口，保留退出码；receipt来自本次真实服务。用本机fixture子进程验证外层控制流，不启动真实实验服务。预算256不改，8h仍仅候选。

E. 在生产0.2的18阶段完整fixture链回归以上修改，并覆盖至少一个真实原生多窗口评分路径、重复辅助内容、跨任务记忆累积。每次调用标fixture，无能力结论。只跑受影响定向回归，不重复全仓。

交付r3含精确diff、探针及完整捕获证据、修正命令和限制。冻结r2日志不改写。完成后停止。
