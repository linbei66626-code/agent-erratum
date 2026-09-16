# 多调用启动器源码门最小补修

2026-09-13。待用户人工转交，尚未派发。只做离线补修，不 SSH、上传、部署、启动服务或调用模型。

## 真实断点

实验室已安装核验过的累计补丁。实际运行生产 `letta_local.py start`，在创建 Letta 进程前失败：`RuntimeError: fixed vendor git commit differs or tracked files are dirty`。HEAD 正确，tracked 改动恰为 manifest 对应的 agent/message 两文件，shim 字节也正确。`start()` 先无条件调用 `source_check()`；后者仍拒绝所有 tracked dirty，因此根本到不了 opt-in 验证与 bootstrap。源码门对应 `scripts/deployment/letta_local.py:235-258`，调用点约 392 行。

证据：`transfers/lab-multicall-service-20260913-r1/remote-evidence/20260913T031758Z-af9f37/`、同目录外 `postcheck.txt`。数据库曾启动，现已停止；没有 Letta 进程、receipt 或模型请求。已有启动接线测试 mock 了 source_check，不能证明真实源码门与兼容模式共存。

## 允许修改

仅 `scripts/deployment/letta_local.py`、`tests/test_ae_multicall_deployment.py`，新增独占结果目录。先冻结改前字节和摘要。其余生产代码、测试、固定上游、manifest、旧交付与 raw 保持。

## 要求

1. 保留无 opt-in 的原有严格行为（包括 migrate）；正确显式 opt-in 时，源码检查能够接受已经由现有严格 manifest/gate 验证的指定补丁。不要删除 dirty 检查，也不要仅靠路径白名单允许任意内容。
2. 复用已有 support 加载、manifest 和 gate 校验，绑定 manifest checkout 为实际 `Deployment.source`。HEAD、原 EXPECTED 文件摘要、隐藏配置拒绝检查全部保留。额外 tracked 修改、暂存修改、删除/重命名、错 HEAD、改坏补丁或缺 shim 必须在 Popen 前拒绝。正确补丁文件的完整字节必须与清单一致；使用真实 Git 输出正确处理索引和工作区差异。
3. 不通过改 Git index/commit、删除或移动 `.git`、隐藏 dirty 状态、改用另一目录冒充服务来源、禁用检查来接受。不得放宽白名单环境、伪造 receipt、改变 manifest 生命周期或研究协议。
4. 增加组合测试：真实 clean pinned checkout 与真实已核 patched checkout，使用真实 `source_check`、真实 manifest/gate 走生产 start 的前置链。可以在最终 Popen 设置明确离线观察点，证明合法情况到达该点；所有拒绝案例 Popen 未调用。不得 mock source_check、Git dirty 输出或 manifest 校验来证明合法情况。
5. 至少覆盖：legacy clean 接受/dirty 拒绝；正确 opt-in patched 到达启动点；未补丁树/错 profile/manifest 缺失或错误/manifest 指向另一 checkout/补丁字节坏/缺 shim/额外 tracked 修改（含 staged）/错 HEAD 均拒绝。测试使用已有显式源码路径约定，不依赖隐藏的 Mac 默认目录；变异仅在临时树内。

## 交付

给出两文件 diff、改前后完整 SHA、定向部署测试和相关 bootstrap 回归日志、真实 start 前置链正反例。不重复已有多调用核心测试来充当启动验收；全量仅在变化或失败确有需要时运行。说明仍未实际启动服务，离线到达 Popen 不等于服务健康；本轮只修上述已证实断点，不声称后续 bootstrap 已通过。
