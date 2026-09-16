# stack已加载但多调用保留未激活：只修实际服务链

## 已确定的有效性结论

r4封存现场完整重审 input-audit-sealed-reaudit-r2.json 已越过历史阶段误判，报tool_call_count_changed。原始337生成响应中10批多调用，共39调用，后续历史只保留各批第一个，29个丢失（6 memory_update、23 longitude_latitude_to_distance）；不是“10批各2个”。细节见transfers/ae-deepseek-re-complete-20260916-r4/multicall-loss-diagnostic.json。未据此断言所有相关偏好永久丢失，但既定执行轨迹已被改写，不能作有效R/E比较。

服务进程11221：AE_LETTA_PATCH_STACK_PROFILE=ae-no-compaction-stack-1，AE_LETTA_MULTICALL_PROFILE缺失；实际服务日志存在Truncating to first tool call。根因：PatchStackRuntime.env给模块SHA/support但未给运行时激活声明；multicall_decision未声明则返回NOT_DECLARED，letta_agent_v3保留首调用。加载正确源码不等于策略激活。

## 最小补修范围

只修stack启动到运行时multicall策略的激活/验证链。读取真实multicall_decision与preserve_tool_call_id依赖，确保stack profile独立、受核验地启用完整批次接收与完整ID保留。不要盲加一个环境变量后只测env；相关依赖/manifest校验必须真实可达。保持stack与旧multicall单独opt-in互斥，不要求用户同时传两个互斥开关。父进程export不能隐式启用。

保留parallel_tool_calls=false、工具串行执行、不增加并发、不改变模型/提示/R-E/预算/禁摘要。stack声明但运行策略未激活时明确拒绝启动/发送，不静默退回截断。默认非stack/旧协议行为按原合同保持。

验收必须从真实launcher生成的子进程环境经过真实bootstrap和真实Agent响应处理链：离线脚本化provider返回至少两条已声明client工具调用（含两条memory_update），断言两条都交给驱动、各执行一次、两个回包带正确ID进入下一次真实SDK HTTP请求；长ID保留全文。仅方法体、manifest摘要或env断言不足以验收。增加缺声明/未知版本/不完整配置拒绝反例。可只替换最终HTTP层和数据库等必要外部依赖，不能stub被验收的多调用决策或响应解析。

只跑对应定向测试和必要受影响回归一次，不全仓、不联网、不部署、不调付费模型。列出实际变更和重建stack/重启/新回执要求。此次任务只交付修复与证据，不自动重跑实验。

## 历史结果处理

原r4仍INVALID，原始记录不改。不要把丢掉的调用从审计预期中删除，也不要事后补执行并拼接旧轨迹。新实验前Codex需在真实部署环境做无付费的多调用接收验证，再决定新目录完整重跑。此处不承诺修完后旧审计其余所有门都已通过。
