# NativeVita真实前置单点接线

现场r3部署已成功：新Letta健康、字节策略与真实回执校验通过；模型调用0。真实CLI PREFLIGHT却报 `NativeVitaError: unknown explicit transport profile`。

位置 `ae_vita.py:147-150`：仅接受local-vllm及旧COMPAT_PROFILE。此文件本地与现场一致，不是部署遗漏。请显式支持已经声明的DeepSeek profile与模型，沿NativeVita初始化及辅助模型配置保持同一出口/模式/参数契约；不要把DeepSeek伪装为旧profile或Qwen模型。旧路径保持兼容。

验收仅真实CLI `--stage preflight --exploratory-capacity-option`，使用现有DeepSeek候选、真实dataset/source、新输出目录（禁止联网）。不得mock NativeVita初始化、替换profile或绕过校验；遇到紧邻的模型配置硬编码一并接通。旧路径仅相关定向回归，不跑全仓。交付小diff和preflight完整结果、精确同步文件，完成即停，不SSH/部署/调用模型。

现场证据 `transfers/ae-deepseek-deploy-20260915-r1/`。本任务不修改已部署服务补丁/manifest/receipt、不扩展审计或实验。
