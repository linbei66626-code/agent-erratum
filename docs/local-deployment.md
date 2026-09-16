# AE-01 本地模型接通准备

## 最新旁路诊断 · 4B 基础能力试跑（2026-09-11）

已授权另跑一次 Qwen3-4B-Instruct-2507／65536 窗口、只读正确偏好／无历史的 t4 能力探针，**未通过，runner INVALID**。最大输入 20765，最后输出到 2048 上限；实际零订单却声称已下单、地址错误、虚构长订单号。不是 R/E 结果。模型／Letta／PG 已停、GPU 0 MiB，平台已停止，释放时间 2026-09-18 12:52:01；旧 8B 和实验数据未删。

新模型目录为服务器 `models/Qwen3-4B-Instruct-2507-cdbee75f/`，逐文件固定 revision 校验通过；独立启动器 `scripts/deployment/start_vllm_qwen4b.sh`，不能用下方旧 8B 启动命令冒充本次配置。主 R/E 配置未改。本地 fixture 全套 188 项通过（1 项跳过），真实环境离线 preflight 本地／服务器均 PASS，不代表模型成功。

**已下载、单独解压并本地验收，不是下方的 8B 包**：`results/ae-capability4b-evidence-20260911-r1.tar.gz`，1528130 bytes，48 文件。SHA256 `2ca75f0dcb1b4791c2fd138c03613f1584f5966f2fec3de9ad5ae7a3ba0f6ad9` 与服务器记录一致；逐文件字节均相符，无缺失／额外文件、不安全路径或链接。原始 INVALID 和完整输入审计未完成的边界不变；未覆盖代码或 raw。旧实例本轮查看已停止，释放时间 2026-09-18 16:52:12。后续转实验室的初检及当前待办见 [迁移入口](lab-migration.md)；准确经过见 [研究当前状态](</Users/lrc/Documents/Obsidian Vault/论文阅读/80-实验与复现/Agent场景多轮修正/Agent场景多轮修正-当前状态.md>)。

以下为此前 8B 运行／恢复记录，不能替代上面的最新状态。

## 当前任务运行结果 · 2026-09-11（INVALID；证据已本地验收）

**当前任务仍仅为 t4–t5 的 rewrite / erratum 接通调试，不是完整 benchmark。** 真实 r2 已运行并因容量门停止，整体 INVALID，见下方实测记录。服务器独立 `.venv-vita` 已安装；原生无模型预检通过，本地与服务器全套 **158 项测试均通过**，分别 4.030 秒、10.751 秒。集成测试使用真实环境、User 和 Evaluator，但回包为 fixture 且阻断 socket；它与 r2 真实模型请求是不同证据。

### r2 已观察到什么

- 运行区间：`2026-09-11T02:35:21.846114Z` → `2026-09-11T02:39:42.802643Z`。rewrite、erratum 均完成 `sub_U000828_4` 的对话和原生评分流程；这只描述流程完成，不表示任务答对或语义审计通过。
- 进入 t5 时，rewrite 新增历史请求的输入为 **31501 tokens**，超过 `max_prompt_tokens=28672`，代理在模型推理前拒绝。窗口 32768、统一输出预留 4096；即使只按该请求实际 `max_completion_tokens=2048` 预留，`31501 + 2048 = 33549 > 32768`。没有静默截断、改窗口或换模型重跑；t5 两臂配对未完成。
- 拒绝前共 **18 次完成模型响应**：14 次 `agent_or_unknown`、2 次模拟用户、2 次评分器；这些已完成响应的 `/tokenize` 与实际 `usage.prompt_tokens` 均相符，没有 `finish_reason=length`，最大已完成输入 27490 tokens。这里只核 token 与完成状态，不据此推断偏好更新正确或完整输入语义有效。
- 代理拒绝后，框架内部又尝试数次、得到 503；AE driver 未自动重试，代理 fail-closed 后没有再向模型转发推理。**不能宣称所有层零重试**。
- r2 **整体 INVALID**；独立输入审计在 `proxy_blocked_rejected_or_incomplete_response` 提前失败，其余完整输入检查尚未完成。不能把已完成 t4 或部分 raw 升级为 R/E 语义有效结果，也不发布评分。记忆提交计数 R=1、E=4 仅为调试计数，不比较策略优劣；`scientific_result`／`scientific_success` 保持 `null`。
- 任务 r1 的推理前配置失败记录仍保留，没有任务推理或科学量。本轮全部原始记录继续保留；**此前已完成平台关机，本次证据包也已下载并本地验收**。本次未重新查询平台、启动服务或新跑任务。

raw 已本地保全，下一步核容量构成，再讨论后续方案；不自动扩大范围、窗口或切换新模型。模型 `max_position_embeddings=40960` 只是原生配置上限，不是已验证的 t4–t5／完整任务容量。

### 本轮部署配置与收尾状态

- 本轮窗口为 **32768 tokens**；Agent 输出 2048，模拟用户／评分器输出 4096。三个角色均使用现有 Qwen3-8B，仅做接通调试，保留模型默认 thinking、原生 chat template，不扩 RoPE、不静默裁剪。
- 当前本机路由：**Letta／三角色请求 → 审计代理 `127.0.0.1:8000` → vLLM `127.0.0.1:8180`**；Letta 服务为 `127.0.0.1:8283`，专用 PostgreSQL 为 `127.0.0.1:5543`。全部 loopback，不开放公网。任务配置的 `model_origin` 和 Letta 的 `VLLM_API_BASE` 都指向代理，不能直接指向 vLLM。
- 旧 DB 中全局 base provider `vllm` 保留 `8000/v1`。环境变量同步只创建缺失 provider、不更新旧行；官方 PATCH 仅给 `base_url` 返回 422，补必需 `api_key=EMPTY` 后受 actor 组织范围约束返回 404，GET 复核仍为 8000。因此采用本机端口互换，**不绕过访问控制改 DB、不删除 provider、不重建原库**。源码依据：`letta/schemas/providers/base.py:276–280`、`letta/services/provider_manager.py:187–193,552–584`、`letta/orm/sqlalchemy_base.py:891–895`；GET 的全局读取回退见 `provider_manager.py:411–436`。
- vLLM launcher 当前接受 `RUN_ID [8192|32768] [8000|8180]`，本轮显式用 `32768 8180`。Unix IPC 原长路径问题已修为每次新建短 `TMPDIR=/tmp/ae-vllm.XXXXXXXX`；缓存和证据仍在数据盘，日志记录实际 IPC 路径。旧日志、失败现场、PG14 保留目录与现用 PG15 都不覆盖。
- 随后遇到固定 Letta 的启动依赖问题：已有英文 `punkt_tab` 可实际加载，但 `nltk.download` 无超时地刷新远端索引。旧启动 PID 4719 经普通 SIGTERM 未退出，核完整命令确认是本次尚无健康端口／任务的进程后单独结束；随后专用 DB 正常停止、恢复，数据和日志保留。新增显式 `--bounded-nltk-startup`：只在首次启动下载内限制 `nltk.downloader.urlopen` 单次 I/O 为 10 秒，完成即恢复，不改全局网络设置、模型 HTTP 或上游源码。默认关闭。这是部署适配，不冒称官方开关或总启动时限。
- 新启动记录 `deployment/runs/20260911T023349Z-0cbc1f`：健康 0.16.8，PID 5208；实际本地资源加载、此次 NLTK 下载返回 `true`、hook 已恢复，不能声称此次必然触发过超时。运行实证见 `deployment-logs/nltk-bootstrap-observed-20260911-r1.log`，不同于 fixture 输出。
- 部署助手 `stop` 正常成功，记录 `deployment/runs/20260911T024333Z-692d15`，停止所记录的 Letta／专用 DB 并保留数据；核明本轮模型／代理 PID 后发送 SIGINT，随后 `ps` 无模型、代理或任务进程，GPU 为 0 MiB / 0%。记录于 `deployment-logs/task-final-service-state-20260911-r1.log`。平台另行确认“已停止”，停机时间 2026-09-11 10:53:51，释放时间 2026-09-18 10:53:51；没有释放实例或删除数据。
- 用户明确自行管理服务器预算，**不再沿用原 ¥10 cap 或强制定时预算卡**；超时仍不是远端计算停止证明。授权范围依旧只到 t4–t5 wiring，不自动扩展 t4–t12，也不自动重试失败任务。

驱动的 `WIRING_COMPLETED_AUDIT_PENDING` 不等于 VALID，本次更未到达该完成状态。原生评分回包只保留作调试；本节是当前恢复入口，以下带日期的短探针／旧计划均为历史记录，不定义本轮预算或服务状态。

### 本轮证据包与本地验收

用户已下载并单独解压，无需重复下载或上传：

```text
服务器：/root/rivermind-data/agent-erratum/ae-task-wiring-evidence-20260911-r1.tar.gz
Mac 保存到：/Users/lrc/Documents/研究生资料/实验/agent-erratum/results/
文件大小：4304749 bytes
SHA256：81e395f50dc509d0aa708b5cd036dbdcc21bc38f7fe3190f4fa74999806a2fbe
```

本地 [解压目录](../results/ae-task-wiring-evidence-20260911-r1/) 验收：包 SHA 与上述服务器记录一致；217 个普通文件、解包总字节 21566458，逐文件哈希均与包内一致，无缺失、额外文件、字节差异、不安全路径或链接成员，没有 `deployment/private/`、`*.private.log`。关键 r2 result、代理 journal 和停服后的 input audit 均在包内。包含运行代码／配置、测试、两次尝试 raw、完整模型请求响应、部署与停服记录；不含模型、venv、PGDATA 或私密服务日志，不是完整环境备份。

追加核对：r2 plan/result 的 provenance 相同，记录的 9 项代码 SHA 与包内和当前项目一致，配置文件 SHA 一致；poststop audit 引用的 4 个 raw 输入，其 SHA 和字节数全部相符。包内真实 NLTK 启动、Letta 健康／停止、GPU 0 MiB、服务器 158 项测试日志均已读取。**归档验收通过不改变 r2／input audit 的 INVALID，也不代表此前提前停止的完整输入语义检查通过。** 未修改包内 raw 或执行模型；平台停机／释放时点沿用上次现场记录，不冒称此次重新查询。

此前自动下载受限，后由用户手动取回；该待办已关闭。模型、数据库和服务器私密日志仍不在本地包内，应与原始任务证据的已保全状态区分。

### 最简恢复顺序（留供后续授权恢复；不是立即重跑指令）

先核 GPU、端口、已保存进程记录与目录；保留当前 DB 和隔离环境，**用 `start-db`，不再 `prepare-db`／重迁移**。若部署助手报告已有进程记录，先 `status` 核对，不覆盖 PID／凭据。下面在已授权服务器 `/root/rivermind-data/agent-erratum` 执行；两个前台服务各用一个独立终端，不放进 tmux。示例时间戳用于全新日志，已有路径仍会被程序拒绝。

1. 启动已有专用 DB：

   ```bash
   cd /root/rivermind-data/agent-erratum
   python scripts/deployment/letta_local.py start-db
   ```

2. 终端 A 启动模型，显式指定窗口和新端口：

   ```bash
   cd /root/rivermind-data/agent-erratum
   bash scripts/deployment/start_vllm.sh "ae-recover-$(date -u +%Y%m%dT%H%M%SZ)" 32768 8180
   ```

3. 终端 B 先核上游，再启动代理。沿用本轮已选工程限制，不因失败放宽：

   ```bash
   cd /root/rivermind-data/agent-erratum
   curl -fsS http://127.0.0.1:8180/v1/models
   .venv-vita/bin/python -B scripts/ae_01_model_proxy.py \
     --listen-port 8000 --upstream-origin http://127.0.0.1:8180 \
     --model Qwen3-8B \
     --journal "results/ae-01__model-capture__t4-t5__recovery-$(date -u +%Y%m%dT%H%M%SZ).private.jsonl" \
     --context-window 32768 --max-prompt-tokens 28672 --output-reserve-tokens 4096 \
     --max-request-bytes 2097152 --max-response-bytes 16777216 \
     --max-requests 1024 --io-timeout-seconds 180
   ```

4. 代理就绪后才启动 Letta；另一个终端执行：

   ```bash
   cd /root/rivermind-data/agent-erratum
   curl -fsS http://127.0.0.1:8000/v1/models
   python scripts/deployment/letta_local.py start --model-url http://127.0.0.1:8000/v1 --port 8283 --bounded-nltk-startup
   python scripts/deployment/letta_local.py status
   ```

**代理健康检查只用准确的 `GET /v1/models`**，不能访问代理 `/health`、`/v1/models/` 或直接 `/tokenize`；这些不支持的路由会让代理 fail-closed。模型列表查询不启动推理，但占请求数。正式任务日志不要混入合成 chat 探针；已用的 `results/ae-01__model-capture__t4-t5__20260911-r2.private.jsonl` 不能复用命名。

后续方案明确并获得相应运行授权后，先核 loopback 配置，再用全新 `--output-dir` 执行任务 runner；不能直接照旧配置自动重试本次超限。保全任务目录、代理 private JSONL、部署日志和独立审计报告；凭据、PGDATA 及 `.private.log` 不加入对外包。尚未发生的停机／下载不提前登记为完成。

---

## 历史短探针与取回验收 · 2026-09-11 09:05 批次

**真实接通已通过，原始包已下载并完成本地验收。** 上次部署结束已停止服务和关机；本次未查询平台当前状态或启动服务。本节取代后面的旧计划作为恢复入口；旧计划保留用于追溯，不能当成当前安装状态、预算或待执行命令。

- 隔离安装完成：`.venv-letta`（固定 Letta 0.16.8 archive），`.venv-vllm`（vLLM 0.10.0+cu126 / torch 2.7.1+cu126 / transformers 4.53.3），PostgreSQL 15.19 + pgvector 0.8.1。未改原 MU Python 环境或上游源码。
- 仅 loopback：Qwen `127.0.0.1:8000`，Letta `127.0.0.1:8283`，专用 DB `127.0.0.1:5543`。bf16 / 8192 窗口 / 单并发 / eager / hermes tool parser / qwen3 reasoning parser；8192 只已用于短探针，不代表 Vita 全任务窗口。
- 运行 `scripts/ae_01_connection_probe.py --execute`，新输出 `results/ae-01__connection-live__qwen3-8b__20260911-r1`；远端输出 PASS，`task_success=null`。没有第二次探针、Vita 任务或 R/E 质量比较。
- 本轮部署前修复缺 `pg_config`；隔离了 Mac AppleDouble 副文件；将失败的 PG14 专用库改名保留后新建 PG15。版本依据为固定 Letta Dockerfile 的 `pgvector/pgvector:0.8.1-pg15`，以及 PostgreSQL 15 才引入的 [UNIQUE NULLS NOT DISTINCT](https://www.postgresql.org/docs/15/release-15.html)。PG15 来自 [官方 PGDG Ubuntu 源](https://www.postgresql.org/download/linux/ubuntu/)，不改迁移 SQL 来适配 PG14。
- `letta_local.py` 显式选 PG15，缺失或版本不符先拒绝；配置隔离另外拒绝账户级 `~/.letta/conf.yaml`，核五个固定源文件。当前 SHA：`49a00168222637f65c9564955befb51f70e9829bd715977a95d7483be7d5ab04`。
- DB 私密凭据、PGDATA、隔离 venv 均保留在服务器，不下载/打印凭据。当前 DB 是已迁移的 PG15，**恢复用 `start-db`，不能再次 `prepare-db`**。失败 PG14 数据保存在 `/var/lib/postgresql/ae01_letta-pg14-failed-20260911`，对应旧凭据在 `deployment/private/letta-db-pg14-failed-20260911.json`；它们仅作失败现场保留，不是当前服务配置。
- 模型/Letta/专用 DB 已退出，GPU 显存 0 MiB。平台记录停机 09:05:15，定时关机取消。原 ¥10 cap 已由用户放宽；本轮仅完成授权的部署/短探针，没有开启无限时任务。

### 已取回并核验的证据

用户已下载并解压到项目 `results/`，本地路径为：

```text
/Users/lrc/Documents/研究生资料/实验/agent-erratum/results/ae-deployment-evidence-20260911-r1.tar.gz
/Users/lrc/Documents/研究生资料/实验/agent-erratum/results/ae-deployment-evidence-20260911-r1/
SHA256: 303d4d8fd1981f226009212d34f8d85d9022526c924af0f9dda96792224357ea
```

包 SHA 与此前服务器记录一致；100 个普通文件与解压结果逐一比对一致，无缺失、额外文件或不安全归档路径，没有覆盖当前代码。包含 `deployment-logs/`、`deployment/runs/` 的非私密记录、`results/`、配置、脚本与源文件；先前安装失败、两次迁移失败和最终成功均保留。包中没有 `deployment/private/` 或 `*.private.log`，因此不是数据库或完整服务日志备份。

探针的四项代码 SHA 与包内及本地代码一致，配置文件 SHA 一致；部署记录的五项 Letta 源码 SHA 与固定副本一致。已核两个 HTTP JSONL 与结果内 transport trace 一致：一次 `read_probe` 调用、同一调用 ID 的工具回包、最终回答去除首尾空白后与随机标记完全相等，`task_success/scientific_result` 仍为 `null`。vLLM 的两条渲染 prompt 记录分别不含／包含该随机标记，第二条包含工具调用及回包。这只核了本次短探针，不是完整 Vita 请求审计或 Letta→模型的完整 HTTP 抓包。

上次平台显示 **2026-09-18 09:05:15** 释放实例；本轮没有刷新这一时点。证据已本地保全，但服务器上的模型、数据库和隔离环境不在此包内。

### 当时记录的后续恢复顺序（已由顶端恢复入口取代）

1. 先本地补真实任务驱动；后续经授权开机时核已有目录和空间，不重装权重/依赖，不重新初始化 DB。已验收的证据无需重下。
2. `python scripts/deployment/letta_local.py start-db` 启动专用 PG15。
3. 在单独前台终端运行 `bash scripts/deployment/start_vllm.sh <全新运行标识>`，确认 `/v1/models` 可用，再运行 `python scripts/deployment/letta_local.py start`。
4. 进入真实任务之前，先补 t4–t5 最小环境/模拟用户/评分驱动和实际输入检查，不直接运行 t4–t12。若另跑诊断，必须用全新 output-dir，不覆盖 `20260911-r1`。
5. 结束时用部署助手 `stop` 停 Letta/专用 DB，核本次模型 PID 后正常停止 vLLM，再下载日志和平台关机。不依赖客户端超时停止远端计算。

---

## 历史计划 · 以下保留的是 2026-09-10 未接通时的记录

> 2026-09-10 21:09 更新：用户要求暂停，服务器已关机，未运行模型探针。已删除不用的 Qwen2.5-14B 八个权重分片（约27GiB），Qwen3-8B及旧结果/脚本保留。独立 uv bootstrap 成功，Letta源码已上传核验，但依赖安装报 psycopg2 构建失败，不能视为完成。vLLM wheel 上传最终完整性未核；部署脚本仅本地准备，未执行。远端 `agent-erratum/deployment-logs/` 仍待取回。下面原部署计划保留；原 ¥10 cap 已被本次用户放宽，但暂停后不得自行重新开机。

状态：**本地代码／静态部署核对，不是部署成功记录**。先接通一次小工具，不运行 Vita 任务，不比较 R/E，不产生科学结论。服务未安装，下面不是一键开机／安装脚本。

## 1. 2026-09-10 已实际核对的服务器

经用户授权，通过 GPUHome 控制台启动 `y690ukfp7va1qs9y`，在 Jupyter 终端执行只读检查。20:26:10 控制台确认已停止；开机不足 6 分钟，按 ¥1.89/小时估算不足 ¥0.19，**未查询实扣账单**。本次设过的 20:30 定时关机已取消。没有安装依赖、加载模型或跑推理。

| 项 | 现场输出 |
|---|---|
| GPU | NVIDIA GeForce RTX 4090；49140 MiB total，0 MiB used，48509 MiB free，utilization 0% |
| 驱动 | 580.119.02 |
| Python / torch | 3.11.13 / 2.7.1+cu126 |
| transformers / tokenizers | 4.57.6 / 0.22.2 |
| 当前 Python 未安装 | vllm、letta、letta-client、openai、fastapi、uvicorn |
| 模型目录 | `/root/rivermind-data/kv-edit-mechanism/model-cache/Qwen3-8B` |
| 权重索引核对 | 5 个分片均存在且非空；分片总字节 16381516776；未做权重哈希／加载验证 |
| config.json | qwen3，bfloat16，36 层，8 KV heads；max_position_embeddings=40960，rope_scaling=null |
| 可用空间（df -h） | 数据盘约 4.5G，系统盘约 28G；不是控制台 50GB 减已用值的简单结果 |
| TCP 监听 | 22/sshd、8888/jupyter-lab、8080/node；未发现现成模型服务 |

以上是该时点观测，不保证下次开机 GPU 仍空闲。模型配置中的 40960 **不是已经验证的任务可用窗口**。探针的 8192 只是短工具回路的工程设置；完整 t4–t5 的实际请求、历史和输出容量还要另核，不静默截断或扩展 RoPE。

## 2. 最少改动的部署分工

```text
AE 接通探针（标准库 Python）
  → 本机 Letta HTTP 服务（独立环境 + 专用 PostgreSQL）
    → 本机 Qwen HTTP 推理服务（独立环境，共用现有权重目录）
  ← read_probe 工具请求 → 本地返回随机标记 → Agent 回答
```

- **保留旧 MU 环境**：不在现有 Python 上直接升级 torch/transformers，不复制或修改权重，不动 MU 的代码、raw 或阈值。
- **模型服务**：vLLM 是候选；尚未选定并验证 Linux wheel/torch 的兼容版本、工具解析器与 chat template。因此本轮不提供未核实的 `pip install vllm` 或启动命令。服务只监听 loopback，显式 served model name、窗口、输出限制；不开放公网端口。
- **Letta 服务**：固定 archive commit `56ba9c25552605eec89de8ed3dc6394b625c1993`，Python 3.11–3.13，独立环境。该旧版已弃用、处于维护模式，活跃开发已迁移；本项目只用于隔离研究。DB 主路径依赖 PostgreSQL；按原容器基线保留 pgvector，不根据遗留 sqlite extra 宣称 SQLite 能跑。
- **Vita + AE 驱动**：之后另一个环境；第一次小工具接通不需要 Vita。Letta server 的 `uvicorn==0.29.0` 与 Vita 的 `uvicorn>=0.34.0` 冲突，不能硬装同一环境，也不修改上游依赖来迁就安装。
- **无需额外 embedding**：本探针不检索，不设置默认 embedding；运行前核返回配置确实为空。不要带入旧的 cloud API keys、Rewrite/RAG 或后台记忆配置。
- **空间**：数据盘不足以再下载一套模型；先做 Linux 依赖解析和安装体积预估，再决定隔离环境落点。不得自动清理其他项目或扩容付费磁盘。

现有固定源码中，`VLLM_API_BASE` 可注册名为 `vllm` 的 provider；它访问 `/v1/models` 并读 `data[].id`、`max_model_len`。配置示例的 `vllm/Qwen3-8B` **须与实际注册 handle 相符**，不是根据目录自动推导的结果。

## 3. 接通探针的边界

配置：[ae-01__connection-probe.local.json](../configs/ae-01__connection-probe.local.json)。两项地址都是在服务器上运行探针时的 loopback，不是 Mac 上直接可用的远程地址。

先生成计划预览（不联网），服务部署后才显式执行。成功须同时满足：模型／Letta 配置匹配；模型实际提出 `read_probe`；以它给出的调用 ID 回传工具生成的随机标记；随后正常结束并答出该标记。只回答正确、只返回 HTTP 200 或只 `end_turn` 都不够。

这不是偏好更新测试：为了查接口，提示会明确要求调用测试工具；没有真实用户、Vita 数据／工具或准确率。首次真实调用产生的 agent ID 和日志保留，不自动删除 agent，失败不自动重试。

探针的请求日志记录客户端↔Letta 和模型列表查询，**不是 Letta↔模型的完整请求抓取**。后者仍是进入实际任务前的待办。端口和 API 配置正确也不证明 KV 命中或加速。

socket 超时、步数和客户端 HTTP 请求数用于及时停止客户端，不是完整的服务器计算预算保证：超时后远端可能仍在计算；后端也可能有内部重试。实际运行时仍要保留平台端定时关机，以及用户授权的总预算 ¥10（包含本次检查、后续租机及任何辅助模型）。

## 4. 后续上传／输出约定

本轮没有上传。部署环境尚未解析锁定，所以暂不给“现在即可正式运行”的命令。

下一次接通仅需上传 AE 的 `ae_adapter.py`、`ae_http.py`、`ae_probe.py`、`scripts/ae_01_connection_probe.py` 和配置；不上传 Vita 全量数据，不下载第二份权重。依赖部署资产核准后另附完整清单。

一次接通使用全新输出目录，至少取回配置／计划、HTTP JSONL、bridge trace、result；失败也取回。记录服务源码版本、启动参数和实际请求设置后，才安排 t4–t5 的真实任务接入。

## 5. 静态来源（不是运行验证）

固定 Letta 源码目录：`/tmp/ae-letta-QaM3ld/letta-v1`；Vita：`/tmp/ae01-vita-8WHDvk/source`（`f60169e89f30499cb7883f3dad76bd03facc908d`）。临时副本可能被清理，以 commit 定位。

- Letta 实际 provider：`letta/schemas/providers/vllm.py:18–59`；这里是包，不是并存的旧 `letta/schemas/providers.py`。
- VLLM_API_BASE：`letta/server/server.py:280–295`；窗口／输出配置：`letta/schemas/agent.py:252–268`、`letta/schemas/model.py:214–261`。
- 窗口上限需客户端校验：`letta/server/server.py:1525–1531` 未启用 provider 最大窗口拒绝。
- 依赖冲突：两源码的 `pyproject.toml` 中 server extra / dependencies。
- DB：`letta/server/db.py:21–58`、`letta/database_utils.py:97–118`、`Dockerfile` pgvector 基镜像。
- embedding 可空：`letta/server/server.py:588–600`；健康端点：`letta/server/rest_api/routers/v1/health.py:18–45`。
- Qwen thinking 不应由 Letta 的 `enable_reasoner` 推断；当前普通 OpenAI 路径没有已核准的 Qwen 模板透传。部署时显式记录服务侧设置，不因首次失败静默改参。
