# 多调用部署：只补两个测试辅助函数的 pinned 路径传递

待用户人工转交，未派发。r5 的生产/receipt 离线验收保持通过。本任务仅是实验室部署前的测试可移植性适配，不开启下一轮协议修改。

已复现证据：`transfers/ae-multicall-deploy-20260913-r1/test-path-probe.json`。设置 AE_LETTA_SOURCE 为真实新路径、模拟 Mac 默认 /tmp/ae-letta-QaM3ld/letta-v1 不存在时，两 helper 都仍访问默认目录而退出；同一生产生成器显式 pinned=新路径立即通过。旧目录没有移动或修改。

仅允许修改：

- `tests/test_ae_multicall_letta.py::write_production_manifest`：把已解析的 BASELINE_SOURCE 显式传为 build_manifest(pinned=...)。
- `tests/test_ae_cloud_re_pair.py::multicall_manifest`：遵循现有 AE_LETTA_SOURCE 约定，解析环境值或保留历史默认后显式传给 build_manifest(pinned=...)。显式缺源必须失败，不 skip。
- 两文件内与此有关的最小验证；新增独占 `results/ae-multicall-test-portability-20260913-r1/` 保存差异、改前后 SHA、日志。不要改生产生成器、策略、审计、bootstrap 或任务配置。

验收：用真实 alternate pinned 与 patched 路径验证两个 helper，不依赖旧 Mac 默认存在；显式错误路径有界失败；现有 104 项兼容/部署与 96 项 RE/辅助禁网通过无 skip即可，不重复 571 项全量。保留生产代码/配置/旧测试原件/各轮交付与核验证据的字节；测试改后候选包需重新生成并核 SHA，不覆盖旧候选包。

不要伪造服务器 /tmp 路径、添加 symlink 绕过、复制常量或放宽来源检查；不 SSH/上传/部署/启动服务/联网下载/调用模型。完成交付后停止。
