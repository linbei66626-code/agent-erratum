# AE-01：云单 t4 统一出口节流（2026-09-12）

针对 r2 真实单 t4 的 TPM 限流（15 个 HTTP200 chat 后第 16 个 chat 被上游 429/50602 拒绝），
新增**保守低频单并发**的出口发送间隔。本轮只做本地编码与离线验证：未 SSH、未启动服务、未读 key、
未联网、未重跑实验。

## 机制（仅 POST chat）

- `CloudConfig.pace_seconds`：默认 `0.0` = 旧行为（**不写任何 pace 记录**，journal 保持原五事件
  `client_request→normalized_request→upstream_request→upstream_response→cloud_summary`）。
- 新候选 `configs/ae-01__cloud-transport__siliconflow.capability-pacing-candidate.json` 取 `65`。
  开启后每次 POST chat 在**上游发送前**等待，使相邻**发送时刻**间隔 ≥65s；**首次发送也冷却 65s**。
- 对所有 POST chat 一视同仁：agent / user_simulator / judge 都走同一出口，不依赖角色识别。
- **GET /v1/models 不节流、不写 pace 记录**；它只是模型目录查询，不冒称一次 GET 也是推理。
- 等待使用可注入的 monotonic 时钟与 sleep（离线测试用 fake clock，不真睡 65s）。sleep 提前返回会
  循环补足到目标；时钟不前进则显式失败而非照样发送。
- `pace_wait` 记录（仅节流开启时，每次恰好一条）含：`interval_seconds`、`first_send`、
  `wait_start_monotonic`、`target_monotonic`、`scheduled_wait_seconds`、`waited_seconds`、
  `previous_send_monotonic`、`send_monotonic`。审计核：间隔与配置一致、首/后续标志正确、
  `target = prev+interval`（首次 `start+interval`）、`scheduled = max(0, target-start)`、
  `send-start = waited`、`send ≥ target`、前一条发送时刻衔接；缺/重/错配/篡改一律拒绝。
- 旧 profile（`pace_seconds=0`）零等待兼容，无需 pace 记录；现有 fixture 未删除、未 skip。

## timeout 链核对与 900 的依据

| 环节 | 实际值 | 依据 |
| --- | --- | --- |
| driver→Letta 每次 loopback 请求 | 180s（旧）/ 新候选 900s | `JSONHTTPTransport(timeout_seconds)`；`ae_capability.PINNED` |
| 单个 Letta POST 内部上游 chat 次数 | 最多 3 | bridge 发送 `max_steps=3`，Letta v3 `step()` 循环 |
| 代理上游单请求 | 180s（**不放**） | `CloudConfig.io_timeout_seconds` |
| Letta→proxy OpenAI 客户端 | 当前安装 SDK 的默认 I/O 超时（本次 600s）、默认 2 次重试 | `AsyncOpenAI(api_key, base_url)` 未设 timeout/retries |
| Vita 用户/评分客户端 | 当前安装 SDK 的默认 I/O 超时（本次 600s）、`max_retries=0` | `OpenAI(..., max_retries=num_retries)`，本项目 `num_retries=0` |

保守正常单步链估计 `3 ×(65 等待 + 180 上游)=735 < 900`，故新候选 driver I/O timeout 取 900。
600s 只是**当前安装 SDK 的默认 I/O 超时**，不是绝对总 deadline；900 也只是**正常单步链的保守估计**，
不声称在 SDK 重试或全部异常路径下是绝对总时限。上游一旦失败，现有代理 latch 保证不再转发。
未放大代理上游 timeout，避免 65s 等待叠加慢响应越过该 SDK 默认 I/O 超时。

## 精确组合门（不泛化放宽）

- 旧云配置仍 180：`CloudExecutionProfile` 仅当 capability 配置显式携带
  `pacing == {driver_io_timeout_seconds: 900, proxy_upstream_io_timeout_seconds: 180, min_interval_seconds: 65}`
  且 `timeout_seconds == 900` 时放行；其他任何取值拒绝。
- 旧 4B validator（`ae_capability.validate_config`/`PINNED`）**未改**：校验在拷贝上只把这一处已明确的
  运行 I/O timeout 归一化后交给旧 validator；返回的原始配置仍是真实 900，进入 plan/provenance。
- 输入审计精确核这一组合：driver 900、proxy 上游 180、`pace_seconds` 65；非该组合仍要求
  `timeout_seconds == 180` 且 `io_timeout_seconds == timeout_seconds` 且零节流。其他科学门未动。

## 边界（不声称）

- 无 tokenizer，不冒充精确 TPM 预算、不保证不 429、不按 cached tokens 打折。
- 单请求本身超限、其他进程共用账户额度，本方案无法解决。
- 报文、模型、tools、输入/输出 tokens、seed/temperature、任务/评分/轮数/上限均未改。
- 429/网络错误仍 latch、不自动重试。
- 局部离线测试通过不等于完整云 t4 输入审计 PASS；仍需新的完整 run。

实现：`ae_cloud_proxy.py`（pace 字段/时钟/记录/循环补足）、`ae_cloud_audit.py`（pace 记录严格核验与
零节流兼容）、`ae_cloud_input_audit.py`（双形态与精确组合门）、`ae_cloud_task.py`（显式 pacing 组合）。
测试：`tests/test_ae_cloud_pacing.py` 等。日志/SHA/diff 见 `results/ae-cloud-pacing-20260912-r1/`。
