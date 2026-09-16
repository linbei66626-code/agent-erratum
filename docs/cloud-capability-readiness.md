# AE-01：单 t4 云能力诊断运行准备（2026-09-11）

本轮只新增运行配置、离线测试与预检文档；**没有运行模型、没有读密钥、没有连接服务器或发送付费请求**。小合成连接探针已 PASS（2 次推理、1 次工具回传、1 个模型目录 GET），但那只覆盖探针实际发出的报文形态，**不等于 t4 能力通过，也不是科学结果**。本页命令只写不执行；执行下一步需要用户另行授权。

## 交付物

| 文件 | 作用 |
| --- | --- |
| `configs/ae-01__cloud-transport__siliconflow.capability-candidate.json` | 云代理的**待核验运行候选**：v2 profile、原模型、`max_output_tokens=4096`、请求 2 MiB、响应 16 MiB、I/O timeout 180、`max_requests=256` |
| `tests/test_ae_cloud_readiness.py` | 19 项离线就绪检查（fixture／fake opener，无网络无模型） |
| `docs/cloud-capability-readiness.md` | 本页：预检、启动、单 t4、停止、证据回收命令与阻断项 |

未改动的既有物：`ae_cloud_proxy.py`、`ae_cloud_task.py`、`ae_capability.py`、`scripts/`、旧 configs、旧 raw、上游源码、`AGENTS.md` 与依赖。旧连接配置 `ae-01__cloud-transport__siliconflow.letta-probe.json`（256／6 请求）与本轮候选并存，不被覆盖。本轮修正只动上面三个新文件；`results/ae-cloud-readiness-20260911-r1/` 与 `-r2/` 原样保留，本轮新验证写 `-r3/`。

## 请求数预算与 `max_requests=256` 的性质

候选 `max_requests=256` 是**安全截断预算（cap），不是"跑完整"的保证**，也**不是**本轮推导出的总请求上界。本轮只分别验证了三条独立约束，它们都是**最大值**，不是实际必发生次数，因此不能相加、也不能据此声称真实请求数必然更高或必然更少：

```text
驱动自身模型目录 GET = 1
  （scripts/ae_01_capability_probe.py 的 model_transport；连接探针实证全程只有 1 个目录 GET）

任务 bridge 的 POST /messages <= 64
  （ae_task_run.py:174 的 max_stage_posts 硬门；begin() 只在 t4 开头把 stage_posts 归零）

外层用户模拟调用 <= 12
  （能力配置 max_user_exchanges=12）
```

另有一条源码事实（不是数值约束）：建 agent 时 `request.llm_config` 非 None 会直接跳过 `get_llm_config_from_handle_async`，因此 Letta 侧不发 `/v1/models`（`letta/server/server.py:537`；连接 journal 全程也确实只有 1 个目录 GET）。

**本轮未证明总云请求上界**，也未证明任何"下限"。以下都不声称有界：Letta 单个 step 内部的 LLM 请求数（`step()` 的 `for i in range(max_steps)` 至多 3 轮，但固定 `openai` 客户端自身 retry 次数未 pin）、评分器 `evaluate_simulation` 的滑窗调用（窗口数随轨迹长度增长）、工具回包累计输入（只有单条上限）。因此 256 只是"超过即停"的安全截断阈值：触顶时代理会 latch 失败、不自动重试，运行记为 INVALID，**不是**科学失败或 reward 0。若实测逼近上限，需另做一次经审计的预算裁定，**不允许**通过改 `max_rounds`/`max_stage_posts` 等原实验定义来凑数。

`max_output_tokens=4096` 是三个云端角色的统一出口上限：Agent 用能力配置的 2048，用户模拟／评分用 `auxiliary_output_tokens=4096`；代理只做记录的 `max_completion_tokens → max_tokens` 改名，不会把 2048 自动放大，也不会缩小 4096。

## 预检 / 启动 / 代理 / 单 t4 / 停止 / 回收

以下路径为实验室容器 `/root/agent-erratum`。真实命令仅列于此，本轮不执行。**服务器已建库并迁移到 `1c28e167b74f`，恢复只 `start-db`，不要 `prepare-db`、不要重复 `migrate`。** 目录名用下面给出的候选名：若该目录已存在，改用新的 r 编号，不要复用或删除旧目录。

### 0. 离线预检（在任何服务启动之前；无密钥、无网络）

```bash
cd /root/agent-erratum
# 代理 plan：应为 PLAN_ONLY、key_loaded=false、network_called=false
.venv-vita/bin/python -B scripts/ae_01_cloud_proxy.py \
  --config configs/ae-01__cloud-transport__siliconflow.capability-candidate.json
# 能力 CLI 配置 plan：只生成 plan.json，不联网
.venv-vita/bin/python -B scripts/ae_01_capability_probe.py --cloud --stage plan \
  --config configs/ae-01__capability__siliconflow.prototype.json \
  --dataset vendor/vita/tasks-full-a4553e1.json \
  --vita-source vendor/vita/source \
  --output-dir deployment/runs/ae-capability-cloud-plan-r1
# 离线环境 preflight（仍不联网）：断言 t4 目标商品、7分糖规格、工作地址存在
.venv-vita/bin/python -B scripts/ae_01_capability_probe.py --cloud --stage preflight \
  --config configs/ae-01__capability__siliconflow.prototype.json \
  --dataset vendor/vita/tasks-full-a4553e1.json \
  --vita-source vendor/vita/source \
  --output-dir deployment/runs/ae-capability-cloud-preflight-r1
```

`plan` 与 `preflight` 都在启动数据库、Letta 或代理之前完成，且都拒绝已有目录；重复执行同一 `--output-dir` 必须报"已存在"，这是防覆盖设计，不是故障。

### 1. 数据库与 Letta（沿用已验证参数）

```bash
cd /root/agent-erratum
.venv-vita/bin/python -B scripts/deployment/letta_local.py status --project /root/agent-erratum
.venv-vita/bin/python -B scripts/deployment/letta_local.py start-db --project /root/agent-erratum
.venv-vita/bin/python -B scripts/deployment/letta_local.py start --project /root/agent-erratum \
  --model-url http://127.0.0.1:8000/v1 --port 8283 \
  --explicit-llm-config --bounded-nltk-startup
```

`--explicit-llm-config` 只跳过 VLLM provider 自动注册，**不要求模型目录先可访问**：Agent 创建走调用方给的 `llm_config`，已验证允许先启动 Letta 再起代理。`--bounded-nltk-startup` 只把启动期 NLTK 下载的单次 I/O 限为 10 秒，不保证总启动时间。启动返回的 `status=ok` 只证明健康检查，不是能力或任务结论；先 `status` 确认没有残留进程记录再 `start-db`。

### 2. 云代理（此步才加载密钥，授权付费）

Letta 已可先启动；代理只要在真正发消息之前监听 8000 即可。密钥由用户自行录入且保持 0600：

```bash
cd /root/agent-erratum
.venv-vita/bin/python -B scripts/ae_01_cloud_proxy.py --serve \
  --config configs/ae-01__cloud-transport__siliconflow.capability-candidate.json \
  --key-file deployment/private/siliconflow.key \
  --journal deployment/runs/ae-capability-cloud-r1.capability.private.jsonl \
  --listen-port 8000
```

journal 必须是不存在的新文件（上例为 `ae-capability-cloud-r1.capability.private.jsonl`，已存在则换 `-r2`）；key-file 缺失或权限不符时脚本拒绝启动。**只有这一步之后才可能出现付费推理请求，本任务不授权执行。** 此命令是前台进程，停止用 `Ctrl-C`。

### 3. 单 t4 能力诊断（run）

```bash
cd /root/agent-erratum
.venv-vita/bin/python -B scripts/ae_01_capability_probe.py --cloud --stage run \
  --config configs/ae-01__capability__siliconflow.prototype.json \
  --dataset vendor/vita/tasks-full-a4553e1.json \
  --vita-source vendor/vita/source \
  --output-dir deployment/runs/ae-capability-cloud-t4-r1
```

只在第 0 步 preflight 通过且用户明确授权付费后才执行。`run` 只跑 U000828 t4 一次，不进 t5、不跑 R/E、不注入 `dataset_history`、无 `memory_update` 工具。结束状态最多是 `CAPABILITY_COMPLETED_AUDIT_PENDING`；`task_success`／`scientific_result` 在完成真实任务输入审计前保持 null。失败不自动重试，按断点保留原始记录再决定。

### 4. 停止

```bash
cd /root/agent-erratum
.venv-vita/bin/python -B scripts/deployment/letta_local.py stop --project /root/agent-erratum
```

随后用 `Ctrl-C`（或 `SIGTERM`）停掉第 2 步的前台云代理；代理会写出 `cloud_close` 记录，缺它日志检查器不会通过。停止保留凭据、数据库与全部记录。

### 5. 证据回收与检查

```bash
cd /root/agent-erratum
sha256sum deployment/runs/ae-capability-cloud-r1.capability.private.jsonl \
  deployment/runs/ae-capability-cloud-t4-r1/result.json \
  > deployment/runs/ae-capability-cloud-t4-r1/sha256.txt
.venv-vita/bin/python -B ae_cloud_audit.py \
  deployment/runs/ae-capability-cloud-r1.capability.private.jsonl
```

`ae_cloud_audit.py` 只输出 `transport_capture_checked`（结构／字节／usage 一致性），**不是** `VALID`，`task_input_audit_passed` 与 `scientific_result` 始终 null。回传目标为本地 `transfers/lab-cloud-capability-20260911-r1/`（已存在则换 r 编号），逐项核 SHA；原始 private 日志只按原样回收，不改写不覆盖。

## 初稿阻断项（2026-09-11历史快照）

2026-09-12更新：下列第1项的云输入审计入口已实现并完成53项本地定向回归，见 `docs/cloud-input-audit.md`。这不替代实验室离线复核或真实服务链验证；其余运行期限制仍需保留，不把旧入口缺失描述当作当前状态。

1. **云任务后验输入审计入口缺失**：`ae_input_audit.py` 只认旧 vLLM 代理事件链（每个 chat 调用必须恰好 7 个事件，含 `token_gate` 与 `/tokenize` token 证明），云代理 journal 只有 `client_request → normalized_request → upstream_request → upstream_response → cloud_summary`，没有 `/tokenize` 与 `token_gate`，也没有共享的 `proxy_open`／`model_summary` 事件名。因此 `scripts/ae_01_input_audit.py` **不能**审计云端运行。在补上真正的云输入审计前，任何云端结果都必须停留在 `CAPABILITY_COMPLETED_AUDIT_PENDING`。
2. **TPM／准入未核**：账户 L0 为 40,000 TPM，但每个请求的真实输入 token 数要在运行时才由供应商 usage 给出；单并发 429 处理策略未实测。`max_requests` 与 TPM 无直接换算，不能预先保证不被限流。
3. **真实任务正确性未核**：只有 fixture 与合成连接证据，没有 t4 oracle 的真实模型通过结论。
4. **云端字段兼容性只覆盖探针实际形态**：小探针实发的报文里 `user` 存在、`parallel_tool_calls=false`、`tools` 为**非 null 数组**，这些形态确实通过；`tools=null` 只在离线 profile 检查里通过，**真实服务从未收到过 `tools=null`**，因此 `tools=null` 仍是未覆盖项。能力任务的其余字段组合（长 messages、工具回包后批次、辅助角色请求）同样未实发验证，不能声称全部兼容。
5. **输入完整性无硬上界**：工具回包累计与评分滑窗调用随轨迹增长，`max_requests=256` 只能截断，不能证明完整跑完；本轮也未证明总请求上界（见上节）。

## 本轮离线验证

在项目根目录（本地与本页命令同构）运行：

```bash
AE_VITA_SOURCE=<固定 Vita 源> AE_VITA_DATASET=<固定 tasks-full> \
.venv-vita/bin/python -B -m unittest discover -s tests -v
```

新增模块为 19 项，单文件 `-v` 复跑、全量两处结果与最终文件 SHA 见独立收据
`results/ae-cloud-readiness-20260911-r3/readiness-receipt.json`。覆盖：候选 CloudConfig 形状与
provenance、字节／输出／超时上限、2048 与 4096 分别被允许且不被自动放大、4097 与双上限／缺上限被拒
且不触上游、旧 256 连接配置仍拒能力请求、messages／tools／tool 回包／`user` 字段原样透传、`seed`
与未声明字段被拒而非静默丢弃、三条独立请求约束与 256 安全 cap、代理 CLI 默认 PLAN_ONLY 不读密钥、
读取真实连接 journal 证明实发形态是 `user` 存在／`parallel_tool_calls=false`／`tools` 非 null，
以及（仅 fixture 路径的）显式 `llm_config` 不发生 Letta 侧模型目录 GET。

显式 `llm_config` 不请求模型目录的真实依据是固定源码与连接原始记录，不是 fixture；云端生成 seed
的省略已由既有 `tests/test_ae_native_integration.py::test_cloud_native_requests_omit_seed_preserve_real_tools_and_judge`
在真实 NativeVita 生成路径覆盖，这里不重复造断言。

**仍未覆盖**：完整任务输入审计、TPM／429 准入、`tools=null` 与能力任务其余字段形态的真实服务兼容性、
真实模型任务正确性、KV／缓存结论、总请求上界。

最终 3 个新文件与日志／收据的 SHA256 只记录在独立收据
`results/ae-cloud-readiness-20260911-r3/readiness-receipt.json`，本页与收据互不哈希、不自指。

依据：固定 Letta `56ba9c25552605eec89de8ed3dc6394b625c1993`、Vita `f60169e89f30499cb7883f3dad76bd03facc908d`，以及 `docs/cloud-task-compat.md`、`docs/cloud-transport.md`、`docs/lab-migration.md`。传输仍只证明结构，不把连接 PASS 升级为科学 PASS。
