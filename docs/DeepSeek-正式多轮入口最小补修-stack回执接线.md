# 正式多轮入口最小补修：只接通已有 stack 回执

## 目标与停止点

只修正式多轮驱动及其事后输入审计对 stack 回执的接线。已有服务启动链已现场通过，不重做 launcher、tokenizer、容量报告。离线验收后交付即停止；不 SSH、不部署、不调用模型、不执行真实 RUN，不扩大为新审计框架。

## 已完成，勿重复

- 国内端点250K探测：HTTP200，prompt250025/completion16。证据 results/ae-cn-capacity-probe-250k-20260914-r1/。
- 新候选 configs/ae-01__re-multiturn__siliconflow.capacity-250k-measured-candidate.json：context250000，测量250025，local_agent_budget；目标资产身份已接入。validate_config及capacity_decision(config,'run')通过。保持工具guard26214、输出2048/4096、禁止摘要、t4..t12。
- 2026-09-14真实Letta已以ae_no_compaction_stack启动，health0.16.8/ok，启动时PID600。
- 服务自身回执 /root/agent-erratum/deployment/runs/20260914T112533Z-1499a3/patch-stack-load.json；对应manifest /root/agent-erratum/deployment/ae-stack-startup-20260914-r1/letta-no-compaction/stack-manifest.json。
- 本地核验及回收证据 transfers/ae-stack-startup-20260914-r1/evidence/。含远端绝对路径，离线测试应构造相同字节关系的本地fixture，不冒称本机能直接核远端路径。

## 确定缺陷

scripts/ae_01_cloud_re_multiturn.py 的 provenance/CLI 仍只有multicall manifest和receipt入口，并调用ae_multicall.validate_launch_receipt。
ae_cloud_re_multiturn_input_audit.py对应provenance审计也只调validate_launch_receipt。

现场同一真实stack回执：validate_stack_receipt通过；validate_launch_receipt拒绝：
`[receipt_fields] the load receipt fields differ from the reviewed set; unexpected=['new_files']`
见evidence/live-verification.json。不能删new_files或伪造旧回执解决。

## 修改范围

优先只改上述两个生产文件及其直接定向测试；可补必要CLI帮助和短说明。不改ae_multicall校验规则、launcher、服务patch、tokenizer或旧封存文件。

- 为0.3容量/no-compaction运行显式接入stack manifest + 服务stack receipt，复用现有validate_stack_receipt，并记录真实路径与摘要。
- 正式RUN必须有正确stack证据；缺失、错摘要、旧multicall-only回执均拒绝，不回退。两种声明同时给出应拒绝。PLAN/PREFLIGHT缺现场回执可保留明确未验证状态。
- 事后审计按相同0.3协议验证相同stack证据，校验new_files，不只信任driver的通过标记。
- 旧0.2 multicall-only行为保留，不允许其manifest接受stack树。不要更改旧证据格式以“兼容”。

## 最小验收

1. 驱动真实provenance入口接受有效stack fixture并记录证据；事后审计对应入口也接受。允许离线依赖，不mock掉回执校验。
2. 缺回执、摘要不符、new_files篡改/缺失、旧回执冒充stack、双声明均拒绝；旧0.2合法路径仍通过。
3. 仅新增定向测试及直接影响的旧回执回归。不要重跑全仓或17套件，不重算容量、不添加付费探测。

新建独占results目录，给README、最小diff、测试日志及SHA。交付时列出供Codex部署的准确文件及对应命令参数。若必须越出两文件才能接通，先说明具体依赖，不自行扩展大工程。
