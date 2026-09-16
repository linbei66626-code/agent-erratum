# 多调用 r2 收尾：部署接线、补丁身份与测试覆盖

待用户人工转交，尚未派发。先读 `results/ae-cloud-multicall-codex-review-20260912-r2/README.md`、`summary-independent.json`。已通过的identity、原参数回放、声明错误时拒绝等保留；不要重做整套方案。

本轮只离线实现和验证，建议新增独占交付 `results/ae-cloud-multicall-20260912-r3/`，先保存修改前字节。既有r1/r2、两轮Codex核验及原raw全部只读。

## 1. 接通现有启动入口与0.2模式保证

现有Deployment.env会过滤三个AE_LETTA_MULTICALL变量和PYTHONPATH。父进程export无效，真实补丁会not_declared→截成一条。不要改为继承整个父环境。

必要的最小新增修改位置为 `scripts/deployment/letta_local.py`，需要bootstrap承载启用校验时可改同目录letta_bootstrap.py；只允许新增明确opt-in配置/模块路径/校验和相应测试，保留密钥隔离、进程身份检查、原停止/数据库逻辑。不得运行部署入口的写操作。复用实际env与实际启动命令构造方法做离线测试，证明正确profile进入将要启动的子进程，缺失或错误在模型/工具副作用前停止。本实验0.2使用者须能确认实际服务模式，不允许以未声明的旧截断路径降级。

保持普通旧协议兼容可行，但要显式区分本实验必须启用的模式；不能将profile_off→TRUNCATED当成0.2保护通过。不能用parallel_tool_calls=true解决。

## 2. 清单证明指定补丁，不是仅SHA自洽

补read_manifest/verify_letta_gate、生成器/离线校验、RE审计的统一契约：

- patched_files必须恰好包含固定agent/message/shim三个文件，缺、多、换名、错误基线拒绝；逐项核实际基线SHA，不能只查存在。
- 校验固定基线应用**已核完整补丁（含shim）**后得到candidate。可在新临时副本应用并逐文件核对，或用已冻结改后SHA。不能接受原始未打补丁文件自报为patched。
- 生产生成器在上述检查完成前不得写offline_patch_verified=true；CLI显式接收源码/项目/输出路径，并真正使用参数，修README的环境变量与实际入口不符。
- 源码目录磁盘核对与服务实际加载证明分开。移除客户端凭目录环境变量生成“服务已加载receipt”的行为；若提供加载receipt，必须由目标进程产生并绑定实际模块路径/字节/profile和实例，审计核完整文件集。缺服务证据标未核；不得用另一份正确scratch目录冒充实际加载路径。
- runtime检查覆盖本进程实际加载的模块和checkout，不只重算任意manifest目录。

Codex反例必须转为拒绝：清单只留一个文件；生产生成器读取未打补丁的原文件；未启动服务只设置目录变量就获得live-loading验收。保留真实正例、未知schema/空清单等旧反例。

## 3. 修两处测试覆盖

- tests/test_ae_cloud_re_pair.py有两个provenance定义，后者覆盖真实CLI wrapper。只保留生产调用路径，并测试实际被调用，不再手补production provenance。
- 辅助负例目前在transport_capture_check_failed早停、auxiliary=0。另造传输链一致的辅助语义变异，确认实际到达auxiliary并报具体辅助错误。摘要stub仅用于明确标注的辅助局部门测试，不称冻结代码原样回放或全门来源验收；保留旧raw。

其余修改范围沿用r2任务；本任务新增启动入口的必要opt-in接线，不能扩大为重写服务管理器。持续记录真实源码切片与完整运行边界，不以新fixture成功声称服务行为已观察。

## 验收与停止点

优先复跑Codex独立probe的新反例，更新探针预期须保留原场景/字段，不能换掉有问题的入口。正确生产CLI PLAN/PREFLIGHT、实际启动命令/环境的离线构造、补丁基线验证与审计正反例一致；单调用、多调用串行、故障前缀保留、辅助顺序/用户来源/评分检查不回退。做定向及受影响回归，共有部署/桥接改动完成后全量一次。

新交付附完整diff、前后SHA、准确命令、各入口真实调用证据、正反例报告、未覆盖项。不SSH/上传/部署/启动服务/联网下载/调用模型；离线交付后停止。
