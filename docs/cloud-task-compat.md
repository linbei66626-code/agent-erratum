# AE-01：云任务接口适配（2026-09-11）

本轮是工程适配，不是新一轮 R/E。旧配置、旧 raw 和固定 Letta／Vita 源码不改；公共驱动新增显式 opt-in 云分支，旧分支回归测试保留。云候选仍为 `Qwen/Qwen3-30B-A3B-Instruct-2507`。

## 接法与边界

- Agent 创建采用固定 Letta 0.16.8 已有的 `llm_config` 输入。该接口已标 deprecated，但在固定版本仍受支持；`server.py:create_agent_async` 在提供它时跳过 catalog handle 解析。未改 provider 数据库或上游源码。`vllm/...` 是兼容句柄标签，不声称服务器跑了 vLLM。
- `context_window` 为本地 Agent 预算：连接探针 8192、后续 t4 能力诊断 65536；不是 `/models` 返回值、实测容量或 token 预检。原始云模型目录只检查目标 ID 存在且唯一，不插入 `max_model_len`。请求仍可能受账户 TPM 或服务窗口拒绝。
- 服务启动新增 `--explicit-llm-config`：不启用 VLLM_API_BASE 自动注册，其他隔离措施不变。尚需真实数据库／Letta 服务验证存储与运行链，离线 schema／renderer 检查不能代替它。
- 新传输 profile `siliconflow-letta-text-transport-v2-prototype` 保留旧 v1 行为；v2 将固定 Letta 实际发出的 `user` 字符串、`parallel_tool_calls=false` 和 `tools=null` 原样传递。官方公开文档未明确列出前两项，因此仅作为**真实兼容性待探测字段**，不宣称已支持。拒绝 true、seed 和额外未声明字段；不静默删字段求通过。若真实请求被拒，先保留失败再定适配，不自动重试。
- 两个 profile 都只把 `max_completion_tokens` 明确改名为 `max_tokens`，留转换记录。消息、工具参数、tool_call_id、回包、system 文本保持一致。
- `NativeVita` 新增显式云 profile，云端不发送生成 seed，也不调用会把 seed 填回 llm_args 的原生 set_seed；300 仅留作 simulation 元数据，不承诺云生成确定性。旧本地模型分支仍发送 seed=300。真实用户／评分组件、工具环境及终止规则保留，fixture 测试不是模型效果。
- 能力 CLI 的 `--cloud` 是必要显式选择；不能用旧配置启动云分支，反之亦然。保留之前受控当前偏好／单 t4、无历史／无记忆工具和原订单 oracle，不进入 t5 或 R/E。没有完整云任务输入审计时不得输出科学成功。

## 密钥：只由用户在服务器终端录入

固定文件在个人 lrc 容器：

```text
/root/agent-erratum/deployment/private/siliconflow.key
```

用户运行（不是在 Mac 本地直接运行）：

```bash
cd /root/agent-erratum
.venv-vita/bin/python scripts/ae_01_set_cloud_key.py
```

隐藏输入完整 API key，按回车。父目录要求本人所有且 0700，文件 0600，拒绝符号链接／覆盖。真实密钥不写 shell 命令参数、对话、git、更新包或下载日志。脚本只保存，不发请求；Letta／Vita 使用 loopback dummy key，只有云代理读取此文件。

## 已有入口与下一步

1. `scripts/ae_01_cloud_connection.py` 默认只生成计划；显式 `--execute` 才调用已运行服务。合成 read_probe 产生不可预知 nonce，必须真实工具调用、同 ID 回包且最终答出 nonce 才算连通。不是偏好任务。
2. `scripts/ae_01_cloud_render_check.py` 在 `.venv-letta` 运行，拒绝所有 socket；用固定 CreateAgent schema 和 OpenAIClient 实际渲染无工具、调用工具、工具回包后三种请求，不假装调用数据库。
3. `scripts/ae_01_capability_probe.py --cloud --stage preflight` 在 `.venv-vita` 做真实固定数据／工具的离线预检；`run` 留待连接探针通过并确定运行限额后。**连接代理每请求输出 256、最多 6 次，不可用于能力任务 2048／4096 输出配置。** 本轮未建立能力任务的正式云代理运行配置。

本地／实验室最终测试与传输状态统一记在 [迁移入口](lab-migration.md) 和 Vault AE-01 日志。更新包封存时的说明是快照，不覆盖旧包。私钥填写完成后下一步为独立数据库／Letta 与小连接探针；不直接进入主实验。

接口依据：[SiliconFlow Chat Completions](https://api-docs.siliconflow.cn/docs/api/chat-completions-post)，2026-09-11 核对；框架依据为固定 Letta `56ba9c25552605eec89de8ed3dc6394b625c1993` 和 Vita `f60169e89f30499cb7883f3dad76bd03facc908d`。
