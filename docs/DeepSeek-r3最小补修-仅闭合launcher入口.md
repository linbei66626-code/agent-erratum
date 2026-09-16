# r3 最小补修：仅闭合 launcher 入口

2026-09-14。先读项目 AGENTS.md。本轮只修一个生产阻塞，不重新展开 R3-1…R3-6，不重算容量、不重跑17套件、不下载资产。

## 已有进展保留

r3 的 stack manifest、bootstrap 源码解析与 receipt 校验、proxy 独立进程发送门及共享计数入口已实现。Codex核 r3 SHA256SUMS：76项一致。旧 multicall manifest diff 仅时间、共享模块摘要及失效checkout路径更新；三个patched Letta文件及patch/shim摘要不变。保留旧交付，不重新生成它们。

## 唯一要完成的链：launcher → source_check → 子进程配置 → bootstrap

当前 `Deployment.start` 第一行仍是 `self.source_check(multicall=multicall)`，不传 patch_stack。`source_check`只经旧 `bind_reviewed_patch`认可multicall树；没有multicall时拒绝tracked dirty，有multicall时又按旧manifest检查。叠加树修改agent/compact，因此新增stack校验器本身通过不等于launcher允许启动它。

另：`Deployment.env`使用显式白名单，`PatchStackRuntime.env`只加profile/manifest/support/receipt键；没有把补丁所需的以下配置送入服务：

- AE_QWEN_TOKENIZER_JSON / AE_QWEN_TOKENIZER_CONFIG
- AE_QWEN_TOKENIZER_TARGET / AE_QWEN_TOKENIZER_ASSET_TARGET / AE_QWEN_TOKENIZER_REVISION

所以即便加载成功，服务计数仍会因缺身份或资产而拒绝。不能要求用户在父进程export来绕过白名单，因为不会继承。

### 最小实现要求

1. stack opt-in时，source_check对真正使用的叠加树走新合同，包含原固定源码检查与新栈摘要，接受正确叠加树、拒绝未声明修改。旧multicall/default分支不放宽。
2. 明确stack模式与旧multicall启动门关系：bootstrap当前先调用install_multicall_gate再调用install_patch_stack_gate，避免旧门先拒绝叠加树。可以由stack合同覆盖已包含的multicall证据，具体最小接法自行决定，但不可绕过身份门。
3. 增加显式的tokenizer配置传递，记录实际文件摘要/身份；仅白名单传入必需字段，不继承整个父环境。目标资产缺失保持正式运行拒绝，离线测试用明确fixture，不能宣称目标资产已补齐。

### 仅需的关闭证据

通过真实 `Deployment.start` / `source_check` / `env` 和 bootstrap 决策链做一个正常离线用例及必要反例：

- 正确stack声明能走过真实source_check，子进程拿到显式计数配置，bootstrap选择匹配栈；
- 叠加树字节错误、缺必需tokenizer声明拒绝；
- 旧multicall/default路径保留原约束。

仅数据库、进程启动、服务健康检查和provider替换为离线依赖；不能mock source_check/env或直接单测verify_stack_gate来证明launcher接通。当前 `test_the_launcher_refuses_a_stack_launch_without_the_bootstrap_wrapper` mock了source_check，只测缺wrapper拒绝，不能代替正常入口。

若无法在现有离线环境完成真实入口核验，交付具体阻塞，不扩建完整运行环境、不联网、不启动实际服务或调用模型。

## 交付与停止

新独占 `results/ae-multiturn-capacity-no-compaction-r3-launcher-fix/`，短README、精确diff/before/after摘要、上述少量测试命令与日志。不要回填为全部科研条件已满足。完成后停止。

后续仍须核目标tokenizer资产与实际身份、端点容量和现场receipt；当前容量表仍是探索估算（后续schema开销未逐阶段替换，不能当精确容量保证）。这些不是本轮编码任务，不为它们追加一整轮工作。
