# watchdog r1：入口与退出码最小补修

2026-09-13。待人工转交，未派发；仅修改新增的 scripts/deployment/ae_re_query_watchdog.py 与 tests/test_ae_re_query_watchdog.py，新增独占 results/ae-query-watchdog-20260913-r2/。保留旧交付与核验目录，不部署/启服/调用模型，不改循环规则或既有生产driver。

## 已通过

Codex复核交付清单与两新文件9项SHA、独立复跑31项全过无skip。真实轨迹回放和单子进程停止已覆盖。以下是组合入口缺口，见 results/ae-query-watchdog-codex-review-20260913-r1/probe.py / probe.json。

## 1. 新输出目录合同冲突

watchdog要求 events.parent 先存在，否则拒绝且不启动子进程；生产 scripts/ae_01_cloud_re_pair.py 的 ensure_output_dir 则要求 output-dir 不存在，存在就FileExistsError。

当 events=output-dir/events.jsonl 时，任何一种目录状态都无法完成组合。必须允许未来的 events.parent 由真实runner创建，watchdog不能抢先创建或占用该目录；继续拒绝旧events/旧run输出，保留sidecar独占性。明确新run目录约束，不用伪造/移动目录或改生产CLI解除拒绝。

增加真实组合测试：wrapper启动一个调用生产 ensure_output_dir 的离线子进程，生产函数成功创建新run后写events；watchdog能观察并停止/自然结束。另用真实CLI的PLAN（固定数据集/配置/源码，禁网）与wrapper合路径，验证新目录可运行。不能mock ensure_output_dir来证明兼容。

脚本顶部示例的 `ae_01_cloud_re_pair.py run ...` 同步更正为实际 `--stage run`；新交付README说明外层现有shell负责服务清理，watchdog只启动直接Python runner，不包裹一个会遗留孙进程的shell来假称已收尾。

## 2. 子runner失败不能返回0

实测 child sys.exit(7) → sidecar记录returncode=7，但watchdog CLI exit0（RUNNER_EXITED_NO_TRIGGER一律EXIT_OK）。改为仅runner returncode==0且监测无异常时返回0；非零或被信号终止必须非零，sidecar保留真实退出码。停止/超时/监测失败既有退出语义保持；不重启，不把不完整结果改VALID。

用真实子进程覆盖exit0、exit7、信号退出；非零不会伪装成功。明确外层shell不论成功/失败都执行现有cleanup（可用fixture cleanup标志证明，不启真实服务）。

## 验收与边界

保留31项并新增上述入口/退出/清理组合用例，独立保存diff、改前后SHA和日志。仅补这两处，不扩工具范围/周期/阈值、不引入提示干预、不重复全项目测试。本轮候选仍未启用；新止损规则与付费短链试验须后续确认。
