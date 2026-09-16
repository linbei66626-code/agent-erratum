# launcher 单点补修：恢复 stack 源码边界

2026-09-14。仅修下面一个已复现缺陷，不重新展开容量、tokenizer、proxy、审计或部署工作。遵守项目AGENTS.md；离线、不SSH、不部署、不调用模型，旧交付不改。

## 已完成的部分保留

launcher已传入patch_stack、显式传递计数配置并正确选择bootstrap门。Codex核本交付SHA256SUMS：15项一致。无需重做这些接线。

## 唯一缺陷

`scripts/deployment/letta_local.py::Deployment.source_check` 的新stack分支在 `patch_stack.check_source` 后，只记录 `git rev-parse HEAD`，就写source.json并return：

- 没比较HEAD与固定COMMIT；
- 没检查git status及清单之外的tracked修改。

`check_source`仅检查固定小集合和stack文件，不能替代完整源码边界。旧multicall分支的HEAD检查、reviewed_dirty约束因此没有在stack路径保留。

### 已复现反例（只改临时测试副本）

使用现有 `LauncherEntryTests.setUp()` / `_deployment()` / `_runtime()`：

```python
p = case.tree / 'letta/server/rest_api/routers/v1/agents.py'
p.write_bytes(p.read_bytes() + b'\n# offline review: unmanifested tracked change\n')
dep = case._deployment()
dep.source_check(patch_stack=case._runtime())
```

结果：调用成功，未拒绝。该文件不在stack补丁集合中，却属于真实服务执行路径。现有正常fixture自己git init并提交为非pinned HEAD，stack分支也接受了它，因此测试不能证明固定源码身份。

## 最小修改

在stack opt-in路径保留固定HEAD核验与tracked变更白名单；允许正确stack合同内的改动，拒绝任何合同外改动。保留原逻辑对staged/deleted/renamed、隐藏修改等已有约束，不通过放宽身份或改COMMIT来让fixture通过。

若部署支持无.git的封存树，保持现有证据边界，不声称选择性hash等于完整源码身份；不要借本轮扩建新归档体系。

## 最小验收

只增加/调整以下定向例子：

1. 固定源码 + 已声明stack改动正常通过；fixture使用真实pinned Git身份（可本地离线构造/复用，不联网）。
2. 非pinned HEAD拒绝。
3. 上述未声明tracked文件修改拒绝。
4. 原stack内合法修改、错误摘要和旧分支约束仍符合原规则。

复跑launcher入口与直接相关源码门测试即可，不重跑202项/17套件、不重算容量、不追加其他工作。不要mock source_check或Git身份比较。

新独占交付目录建议 `results/ae-multiturn-capacity-no-compaction-r3-launcher-source-fix/`，提供短README、精确diff及before/after摘要、定向日志。完成后停止。
