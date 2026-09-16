# 多调用 r3：仅补启动入口与 receipt 闭环

待用户人工转交，尚未派发。先读 `results/ae-cloud-multicall-codex-review-20260913-r3/README.md` 与 `summary-independent.json`。87+96 项已独立通过；保留已验的调用/ID/记忆/辅助逻辑。本轮仅离线实现，新增独占 r4 交付，先保存改前字节，旧交付、探针、raw 全部只读。

## 三个具体修复

1. **启动器能在普通脚本环境导入策略模块。** `letta_local.py` 的 validate/start 直接 import ae_multicall，但实际脚本 sys.path 没有项目根。按显式项目/support 路径做受控导入并核实际路径/SHA，不靠父进程 PYTHONPATH 或测试预导入，不放宽子进程白名单。新进程测试须使用实际入口的搜索路径且不预导入项目模块。
2. **服务 receipt 绑定本进程实际 Letta 来源。** bootstrap 现在只定位 ae_multicall，checkout/文件摘要仍复制 manifest。必须核实际解析/加载的 Letta agent/message/shim 路径与字节，考虑 bootstrap 进入原 CLI 时的最终 sys.path；原版或另一份 checkout 不能因另有正确 scratch 树而获成功 receipt。只做磁盘检查可明确记为启动前检查，不能标成已加载。保留模型/工具副作用前拒绝；不要求本机缺依赖时伪造完整 agent 运行。
3. **把服务 receipt 引用接进生产 RUN provenance。** 增加明确参数或既有启动结果读取，记录 `{path,sha256}` 到 plan/result；不由客户端拼 receipt 内容。缺失/错误在 RUN 发任何模型请求前拒绝。PLAN/PREFLIGHT 保持可离线并明确未核加载，不以 applied_to_live_server=false 让真实 RUN 绕过。理顺 manifest 与 receipt 的生成顺序，避免先写 receipt 再更改 manifest SHA。审计与生产 CLI 用同一契约。

允许最小修改：`scripts/deployment/letta_local.py`、`letta_bootstrap.py`、`scripts/ae_01_cloud_re_pair.py`、`ae_cloud_re_input_audit.py`；如 receipt 字段/统一门必须调整，可改 `ae_multicall.py` 与清单生成器/离线部署校验及对应部署材料。只补相关测试（deployment、RE、必要 gate 测试）与文档。不得更改 driver 的任务执行语义、memory policy、parallel_tool_calls、ID 策略、模型/预算/评分，或重写服务/数据库管理。

## 一条闭环验收，替代手工拼正例

以新进程和真实生产入口连接：实际启动命令/受控环境构造 → bootstrap 来源检查/receipt → 生产 CLI 读取引用 → 实际 plan/result → 审计。离线允许明确 fixture 运行环境，但不能在测试里手补 production provenance 来声称入口通过。正例应完整传递；反例至少包括：干净搜索路径、原版 Letta 配正确 scratch 清单、缺 receipt、改 SHA、实例/路径不一致。保留未启用的旧路径，0.2 不静默降级。

复跑 Codex 当前三个反例，原场景保留；人工补引用的对照仅作诊断，不能作为生产正例。测试进程调用 gate 产生的是离线 gate receipt，不得称服务进程实测。更新旧 service_receipt_claim 标签，真实服务验证仍留到后续部署阶段。

做相关定向与受影响回归；共享启动/来源代码改后全量一次即可。交付准确命令、diff、改前后 SHA、逐项结果、未覆盖范围。不得 SSH/上传/部署/启动服务/联网下载/调用模型；不自行派发后续任务。
