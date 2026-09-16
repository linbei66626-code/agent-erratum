# AE-01：SiliconFlow 传输层原型

本轮只完成云代理和它自己的记录检查器；**不是 Letta/Vita 云端任务驱动，也没有调用真实模型**。本地全套 216 项离线测试通过，包含新增 27 项专项测试，日志 `results/ae-01__cloud-transport-offline-tests__20260911-r1.log`。原 9 个运行模块及旧 configs 与 bootstrap 封存字节一致；未改 R/E、旧 raw 或上游源码。

## 做了什么

- `ae_cloud_proxy.py`：只向固定 `https://api.siliconflow.cn` 转发；只接收本地 loopback 请求，串行、非流式、无自动重试、不跟随重定向、不继承系统 HTTP 代理。只代理 chat completions 和 models，不调用 `/tokenize`。
- 仅对选定非思考模型 `Qwen/Qwen3-30B-A3B-Instruct-2507` 提供原型配置。输入的 `max_completion_tokens` 显式改名为 `max_tokens` 并记下转换；其余消息、工具、采样字段不作修复。已经使用 `max_tokens` 的请求原字节转发。
- `seed`、`parallel_tool_calls`、`chat_template_kwargs` 以及其他未列入已核范围的参数一律拒绝，不静默丢弃。**文档未列出不等于服务确定不支持**；真实兼容性尚待小探针。旧驱动仍发这些字段，当前不能直接接上。
- API key 仅在显式 `--serve --key-file` 时从用户指定、本人所有的普通 0600 文件读取；拒绝符号链接。传入的 Authorization 不转发，真实 key 只在代理出站时加入。原始正文包含个人资料，journal 按 0600 独占新建；已知 key 的直接／JSON Unicode 转义回显会扣留正文并停止，不把脱敏内容冒充原始字节。不是对任意编码秘密的通用 DLP 保证。
- 请求前后字节、SHA、参数转换、返回 usage、finish reason、模型标识、可得的 `x-siliconcloud-trace-id` 与 system fingerprint 都留证据。固定配置及运行模块 SHA 进入日志。未知 token IDs／独立 token 对齐保持 null；cache usage 若服务返回也只是原始声明，不证明本实验 KV 前缀复用或加速。
- 429、截断、异常 usage、模型标识不符等保留原响应并禁止后续请求；超时不自动重试。`io_timeout_seconds` 是 socket I/O 超时，不是计费墙钟上限。请求数／字节／输出上限只用于本轮接口探针，不能直接用作真实长任务参数。
- `/v1/models` 原样返回，不伪造 `max_model_len`。固定 Letta vLLM provider 却要求此字段，因此模型元数据接入仍是下一步缺口；源码 `letta/schemas/providers/vllm.py` 可核。

## 如何检查（不需密钥，不发请求）

文件上传后，在 lrc：

```bash
cd /root/agent-erratum
.venv-vita/bin/python -B scripts/ae_01_cloud_proxy.py \
  --config configs/ae-01__cloud-transport__siliconflow.prototype.json
```

应为 `PLAN_ONLY`、`key_loaded=false`、`network_called=false`、`task_runner_ready=false`。

```bash
AE_VITA_SOURCE=/root/agent-erratum/vendor/vita/source \
AE_VITA_DATASET=/root/agent-erratum/vendor/vita/tasks-full-a4553e1.json \
.venv-vita/bin/python -B -m unittest discover -s tests -v
```

上述模型回复全是 fixture，不消耗真实 API；真实 Vita 集成测试会阻止网络连接。运行输出保存新路径 `deployment/lab-cloud-transport-20260911-r1/offline-tests.log`，不能覆盖之前的 lab-install 记录。

## 日志检查器边界

`ae_cloud_audit.py` 对云 profile 独立核：序号、开关记录、完整请求事件链、字节／SHA、唯一请求 ID、只允许的参数变化、响应摘要、限额及失败标记。需要完整关闭记录及匹配运行代码版本。返回的是 `transport_capture_checked`，而不是实验 `VALID`；`task_input_audit_passed`、`scientific_result` 始终 null。

旧 `ae_input_audit.py` 的 token 证明与任务语义门未改；后续真正的云任务输入检查还需接入实际 Letta/Vita 轨迹，不能拿这个传输检查器替代。仅有 models 请求的日志可通过结构检查，但完成 chat 数为 0，不证明推理接通。

## 上传、下载与下一步

本轮新文件：`ae_cloud_proxy.py`、`ae_cloud_audit.py`、`scripts/ae_01_cloud_proxy.py`、`configs/ae-01__cloud-transport__siliconflow.prototype.json`、`tests/test_ae_cloud_proxy.py`、本页；另更新 `.gitignore` 防止私密 key/journal 和环境被误收。更新包 `transfers/ae-cloud-transport-20260911-r1.tar.gz` 附逐文件 manifest，包含本地测试记录，不含真实 key、模型、旧 raw 或 venv。当前只有本地实现，**尚未上传／在 lrc 复测**。

上传目标：`/root/ae-incoming-20260911/`；核 SHA、manifest 和旧依赖文件后再部署 `/root/agent-erratum`。新文件目标必须不存在；`.gitignore` 只在仍匹配旧封存版本时更新，不能覆盖用户的新改动。下载：本次新建 `deployment/lab-cloud-transport-20260911-r1/` 的检查记录回本地 `transfers/lab-cloud-transport-20260911-r1/`，无密钥材料。

之后才是用户私密录入 key、有限 API 接通探针；进一步核 seed／工具参数／模型元数据／账户 TPM，再改云专用任务驱动。当前小探针配置上限 6 个代理请求、每响应 256 output tokens，不用于 R/E 或原 4B 能力问题复测。没有模型 revision pin 的云服务不能承诺位级或跨日期确定性。

依据：[SiliconFlow 官方 Chat Completions 文档](https://api-docs.siliconflow.cn/docs/api/chat-completions-post)，本轮核于 2026-09-11：Bearer 鉴权、max_tokens、tools、trace header。文档和旧目录观察不是已完成的真实服务兼容性测量。
