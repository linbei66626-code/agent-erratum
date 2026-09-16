# r4 仅补 receipt 强制校验

待用户人工转交，未派发。先读 `results/ae-cloud-multicall-codex-review-20260913-r4/README.md` 与 `summary-independent.json`。98+96 项已独立复跑通过；路径导入、解析目录约束、receipt 引用保留，不重做多调用/ID/任务/记忆/评分。

## 当前唯一阻塞

生产 provenance(require_receipt=True) 接受任意存在的 receipt 文件；完整 0.2 审计只在 manifest 的 applied_to_live_server=true 时核 receipt。默认 false 下，只有 `{"instance_id":"bogus"}` 的文件仍被完整 fixture 审计判 VALID。实际 CLI RUN 在 false/true 两种情况下都进入 execute_re_pair，靠核验方拦截才停止。

## 最小实施

1. 把已有 receipt 内容校验复用为共同只读检查，生产 RUN 在创建/进入执行与发请求前调用：核 JSON/字段、profile、manifest 摘要关联、instance、来源核对方式、完整文件集、实际路径与摘要。只有命名 receipt 文件不算通过。保留 `{path,sha256}` 引用，不由客户端生成服务 receipt。
2. 完整 0.2 运行审计必须执行 receipt 检查，不能被 applied_to_live_server=false、multicall_loading_verified=false 或缺字段绕过。PLAN/PREFLIGHT 可无 receipt 离线执行，状态明确未核；不能把离线预览当已执行运行。
3. 明确 manifest/receipt 生命周期：先固定 manifest，bootstrap 引用其摘要；RUN 与审计核同一对文件，不靠事后改 live flag 导致摘要循环。

允许修改生产 CLI、RE 审计、必要共同门模块 ae_multicall.py，以及相关测试/清单生成器或部署说明；仅为上述共用校验接线。不改 driver 任务执行、并行开关、ID/记忆策略、模型/预算/评分，不重写服务管理或放宽现有门。共享模块改动要更新必要摘要/清单，保留旧交付与全部核验/raw。

## 一次验收清单

- 实际生产 CLI RUN 的缺 receipt、垃圾 JSON、缺字段、错误 profile/manifest SHA/文件 SHA/路径，在 execute_re_pair 和 transport 请求前拒绝；false/true 两种 manifest 都不能绕过。
- 同样反例进入完整审计均 INVALID，不能只测内部 validator。
- 正例使用 bootstrap 函数实际生成的、明确标为离线 fixture 的 receipt，经生产 CLI writer 传递到 plan/result，再进入完整审计；不得手工补 provenance 冒充闭环。完整运行正例校验已执行可追溯。
- PLAN/PREFLIGHT 无服务正常且未核；0.1 边界保留。旧调用/ID/辅助/评分回归保持。
- 在真实 find_spec 下的受控包路径测试与实际完整 Letta 服务验证分开；点分 find_spec 会导入父包，不声称完全没有 Letta 导入。

先存改前字节，新独占交付目录。做必要定向和受影响回归；共享门改动后全量一次。附旧反例复跑、实际入口调用证据、准确命令、diff/SHA 与剩余运行边界。禁止 SSH/上传/部署/启动服务/联网下载/调用模型；完成离线交付后停止。
