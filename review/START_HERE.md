# 网页端项目评审入口

## 目标
判断 Letta/Vita 持续任务中 rewrite 与 erratum 两种记忆更新方式的差异。范围为 U000828 的 t4–t12，两臂其他条件应可比。优先得到可信的探索观察，不建设通用评估平台。

## 当前方向
DeepSeek 官方 API、非思考模式；候选是 configs/ae-01__re-multiturn__deepseek-flash.sim-eval-budget1024-exploratory-candidate.json。串行、无固定 65 秒等待、请求总预算 1024。附加确定性评估移到 offline_diagnostic，原生评分、对话、工具与数据库证据保留。探索结果不自动等同冻结协议 VALID。

已有 r5 结果评审见 results/ae-r5-result-review-20260916-r1/README.md；两臂原始 reward 均 4/9，但用户模拟角色越界及 t5/t8 评分冲突使这些分数不能直接支持记忆机制结论。

对话中最新监控报告：r6 在 rewrite t6 因 SimulatorProtocolViolation 停止，完成 2/18 阶段，45 次生成响应均 HTTP 200。这是此前监控报告的摘要，本上传步骤没有重新查询服务器，仓库未附 r6 完整原始记录，不能据此独立复核具体失败。需要时向用户索取对应最小原始轨迹，不把摘要当原始证据。

## 优先查看
- scripts/ae_01_cloud_re_multiturn.py：CLI 与配置
- ae_cloud_re_multiturn.py：两臂逐阶段驱动
- ae_adapter.py：Letta 交互与工具回传
- ae_vita.py：原生用户模拟、环境与评分接线
- ae_sim_eval_protocol.py：用户角色边界与附加判定
- ae_deepseek_re_transport.py、ae_cloud_proxy.py：传输合同
- deployment-assets/letta-*：服务补丁及构建声明
- tests/：区分离线替身与真实接口覆盖，不能以测试数量推断线上成功

## 评审请求
请直接检查代码，判断当前实现是否忠实支持上述实验。只将改变实验含义或阻止基本运行的实际问题列为必改；每项给代码位置、证据、最小修法和必要验证。指出哪些机制可以移到人工复核。重点检查用户模拟角色转换、提示构造、STOP 语义与 R/E 对照是否仍然成立。

不要扩建通用评分器、审计平台或枚举假设性加固。先分析，不改代码。材料不足时明确指出具体缺少的文件/轨迹，不猜测模型能力，不假定所有历史修复判断都正确。

## 快照边界
此仓库包含当前第一方代码、测试、配置、补丁、文档和两份结果说明；不包含虚拟环境、模型权重、私有凭据、完整数据、完整 results/transfers 或嵌套上游源码树。因测试引用这些本机资产，不能期待克隆即全套测试通过。manifest 中绝对路径是原环境记录，未为了可移植性伪造摘要或改写身份。

原项目字节未修改。GitHub 副本 README 新增导航，review/ 为上传说明。SOURCE_SNAPSHOT.json 记录所带源文件摘要。历史文档的绝对路径与未带入文件链接可能无法在 GitHub 打开。

共享协作约定见 SHARED_WORKFLOW.md；项目 AGENTS.md 中本地路径对应原工作区，不是网页沙箱的可访问路径。
