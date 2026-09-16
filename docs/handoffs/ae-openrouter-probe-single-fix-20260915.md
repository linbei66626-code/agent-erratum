# DeepSeek：仅修 OpenRouter 容量探针入口

Codex 离线复核：交付 SHA256SUMS 26 项全部通过，tests/test_ae_openrouter_transport.py 13 passed；真实 Nebius 响应通过 response_summary，transport_issues=[]。不要求重做生产适配或回归大套件。

## 唯一已复现阻塞

results/ae-openrouter-integration-r1/probe/run_once_openrouter.py 中 normalize_request(raw, config) 后要求 normalized == raw，否则 return 2。

对于合法内部请求（model=Qwen/Qwen3-30B-A3B-Instruct-2507、无 provider），规范化必然执行 model replace 和 provider add，故该检查永远拒绝。若反过来给 wire 请求，normalize_request 又以 client_routing_not_allowed 拒绝。这使探针无合法可发送输入。13 项生产测试不覆盖该探针 main。

## 修改与验收

- 不修改封存的 integration-r1 交付；把修正版探针放新 results/ae-openrouter-probe-fix-r1/probe/ 目录，或放可维护 scripts/ 入口后封存到新目录。
- 接受合法内部请求；plan 的 request_sha256 继续核原始输入。保存原始与 normalized 请求各自 SHA 和 changes；dispatch 仍传原始输入，由真实代理执行一次规范化。删除“规范化不得变化”这一错误条件，不删除路由保护。
- 使用离线 opener 驱动探针真实 main：合法内部请求恰好发送一次，发送字节等于 normalized；客户端 provider 仍拒绝且零发送；重复 attempt marker 仍拒绝。
- 无需联网/SSH/付费/部署，不重算容量、不修改生产计数或冻结合同。仅定向测试，交付后停止。

## 现场事实供说明引用

nebius 路由已由 Codex 实测：HTTP200，provider=Nebius，同款模型工具参数正确。先前合成 250K 请求在 OpenRouter 被估为 500023 input，HTTP400，无新增账单；不能再原样付费重试。未来容量材料需另作准备，不能给已失败材料套上 verified。
