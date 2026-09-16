# 探索候选收缩：附加确定性评估改为离线诊断（2026-09-16）

按《探索实验收缩执行单》执行：**不修更多审计**，把可选分析从实际实验中拆出来。只做两件事，只验三件事。未部署、未调用模型、未联网、未跑全仓。

- 停止边界：本单取代最近几份继续补修判定时序审计的任务，那些补修不再继续。
- 被取代的补修：p2 复核提出的四个时序/索引位置**未在本轮实现**（见"未做的部分"）。

## 一、改了什么

### 1. 新的独占探索候选（旧候选未覆盖）

`configs/ae-01__re-multiturn__deepseek-flash.sim-eval-budget1024-exploratory-candidate.json`

- 沿用：DeepSeek 非思考、R/E 两臂、t4..t12、角色约束（`ae-user-simulator-role-boundary-1`）、串行 0 间隔、既有工具/容量/工具返回边界、多调用完整接收。
- `max_requests` = **1024**（已有两个受支持值之一）。
- 新增 `deterministic_evaluation` = `offline_diagnostic`。
- 与旧 0.5 候选的差异**只有** `max_requests` 与 `deterministic_evaluation` 两个字段；其余逐字相同（有测试断言）。
- 三个旧候选文件**字节未改**（有测试断言它们不含该字段）。

字段是**经校验**的，不是新 schema 体系：0.5 专属、显式、可选（省略即 `in_run`），未评审取值直接拒绝，0.1–0.4 声明带上它会被拒。没有改审计器，也没有扩 `ae_sim_eval_protocol.py` 的评分功能。

### 2. 两处可选计算在探索模式下不运行

| 位置 | `in_run`（旧候选） | `offline_diagnostic`（新候选） |
|---|---|---|
| `ae_vita.NativeVita.finish` 内的 `deterministic_business_result` | 照常计算并写入 `business_completion` | **不调用**；写 `business_completion: None` 与 `additional_evaluation.status = NOT_RUN_PENDING_MANUAL_REVIEW` |
| `execute_re_multiturn` 末尾的 `separated_evaluation` | 照常生成 `result["evaluation"]` | **不调用**；`evaluation: None`，`additional_evaluation` 明确记未运行/待人工复核，`may_be_read_as_a_pass: False` |

- 运行记录 `result["deterministic_evaluation"]` 写明模式，两个位置由**同一份**已校验声明驱动（CLI 把同一个值传给运行时），不会出现运行时与记录不一致。
- 保留：原生 judge 及其原始输出、每阶段数据库、对话、工具调用、记忆更新、结束原因。
- 逐轮状态记录**原样留下**，但在探索模式下不再采样（它只是可选分析的输入，不作为前置）。
- 旧配置行为不变：`in_run` 是默认，0.1–0.4 根本不能声明该字段。

## 二、验证到哪（只验三件事）

### 1. 新候选真实 PLAN/PREFLIGHT 接受、记录 1024、代理预算一致；旧配置不变

```
PLAN  max_requests= 1024  mode= offline_diagnostic
PLAN  pair_request_budget= {"max_requests": 1024, "counting": "whole pair, every provider role,
                            every task of both arms, counted by the proxy's own per-process counter…"}
PREFLIGHT status= PREFLIGHT_PASS  network= False  model= False
```

产物：`plan-1024-exploratory.json`、`preflight-plan.json`、`preflight-result.json`（复制自离线运行输出）。

代理预算一致性：复用既有 `scripts/ae_01_cloud_proxy.py::bind_request_budget`——代理配置的 `max_requests` 必须等于候选声明，否则拒绝；测试按 1024/256 两侧断言。**代理 ops 配置需按同一 1024 生成**（见同步清单）。

旧配置：`serial-budget1024-candidate.json`（0.4，1024）、`sim-eval-repair-candidate.json`（0.5，256）、`serial-candidate.json`（0.4，256）全部仍被接受，模式均为 `in_run`，文件字节未改。

### 2. 两处可选计算确实被跳过（把两个函数设为抛错，仍能保存结果）

- **驱动层**：把 `ae_sim_eval_protocol.deterministic_business_result` 与 `ae_cloud_re_multiturn.separated_evaluation` 同时换成抛错版，真实 `_run_one_phase` 仍跑完并留下阶段记录（`agent_stop`、judge 输出、raw 证据齐全），`evaluation is None`、`additional_evaluation.status = NOT_RUN_PENDING_MANUAL_REVIEW`。
- **运行时层（真实 `NativeVita.finish`，复用 `tests/test_ae_vita.py` 的离线 fixture）**：
  - `in_run`：计数版被调用 **1** 次，结果含 `business_completion` → 旧行为未变。
  - `offline_diagnostic`：抛错版**未被调用**，`business_completion is None`，`additional_evaluation.ran = False`，而原生 judge 输出（`reward_info.reward == 1.0`、`judging_status == MODEL_JUDGED_DEBUG_ONLY`）照常生成。
- 为了让"是否计算"可被单一开关控制，`ae_vita` 改为**经模块**调用 `sim_eval.deterministic_business_result`；这是本轮唯一的实现侧重构，不改变默认路径行为。

### 3. 原有故障仍按原规则停止留证

- 唯一的运行级 `except` 仍是**既有的**成对故障处理器：写入 `invalid_reasons`、发出 `pair_stopped`、状态 INVALID；没有新增 `except`、没有 `pass` 式吞掉、没有任何在 except 分支里跳过故障或继续跑的路径。
- 传输/容量检查（`/v1/models` 目录、Letta 健康、`work_address` 等）逐字保留；测试用一个只回空目录的 provider 触发，故障照常抛出。
- 模拟器越界仍抛 `SimulatorProtocolViolation` 并保留原话；judge 失败仍抛并保留订单。
- 声明式校验（未知模式、0.4 带该字段、0.5 缺协议字段）全部拒绝。

跑过的测试：`tests/test_ae_exploratory_offline_diagnostic.py`（16 项，新增）、`tests/test_ae_user_simulator_evaluation_repair_p1.py`（39 项，判定时序链的既有验收）→ **55 passed / 27 subtests**；小范围受影响回归 `test_ae_vita`、`test_ae_native_integration`、`test_ae_task_run`、`test_ae_deepseek_re_transport_driver`、`test_ae_deepseek_re_budget1024`、`test_ae_user_simulator_evaluation_repair` → **110 passed, 2 skipped, 138 subtests**。未跑全仓。

## 三、下一轮最小同步文件清单与重启

需要同步到现场的文件（共 6 个）：

1. `ae_vita.py`（模式开关 + `finish` 跳过）
2. `ae_cloud_re_multiturn.py`（字段校验 + 模式访问器 + 驱动跳过 + 记录）
3. `scripts/ae_01_cloud_re_multiturn.py`（把模式传给运行时）
4. `configs/ae-01__re-multiturn__deepseek-flash.sim-eval-budget1024-exploratory-candidate.json`（新候选）
5. `ae_cloud_re_multiturn_input_audit.py`——**本单未改**，但现场需与 `results/ae-simulator-eval-repair-r1-timing/after/` 同一版本（判定时序链已在本地通过）；若现场是更早版本，按该目录同步。
6. `ae_sim_eval_protocol.py`——**本单未改**，同上按 timing 目录对齐。

**代理 ops 配置**：必须按新候选的 `max_requests=1024` 生成/确认，否则 `bind_request_budget` 会在启动时拒绝（这是既有的一致性检查，不是新增）。

**重启**：新起驱动进程即加载新代码；无证据要求重启 Letta/模型长驻服务。审计模块由审计进程自己导入。

## 四、未做的部分（明确记录，未自行启动）

- **p2 复核提出的四个时序/索引位置未实现**：指令状态采样仍在 `run_stage` 之后；`tool_events_so_far` 与阶段内工具证据的索引绑定、状态投影与工具结果的交叉核实、以及"无订单阶段/末轮后创建订单"两处误拒，均未修。本单已明确取代那些补修任务。
- 因此本地工作树里 `ae_cloud_re_multiturn_input_audit.py` 恢复为 timing 交付版本；被取代的改动没有留在树里。
- **未跑真实 RUN**：PLAN/PREFLIGHT 是离线阶段；实际运行分支由抛错替身验证"确实跳过"，不是在线跑通。
- 探索模式下 `business_completion` 与 `evaluation` 均为空是**预期的**：附加评估是离线诊断，未运行即未运行，记录明确标注待人工复核，不代表通过也不代表失败。
- t5/t8 的标准冲突与其它口径问题仍按前几轮结论保留，未在本轮处理。

## 五、结论层级

- 实现完成：是（配置字段、两处跳过、CLI 传递）。
- 离线组件验证：是（PLAN/PREFLIGHT 接受 1024 且预算一致；两处跳过以抛错替身验证；故障路径仍抛出）。
- 在线任务跑通：**否**（未部署、未调用模型）。
- 结果分析：不适用（尚无新运行）。

## 文件

- `plan-1024-exploratory.json`、`preflight-plan.json`、`preflight-result.json`
- `after/`（改动后的 4 个生产/配置文件 + 新测试）、`before/`、`diffs/`
- `SHA256SUMS`
