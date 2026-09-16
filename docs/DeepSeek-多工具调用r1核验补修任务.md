# 多工具调用 r1：按已复现断点补修，仍限离线

先读 `results/ae-cloud-multicall-codex-review-20260912-r1/README.md` 和 `summary.json`。原始实现任务的研究定义、预算、不可改旧raw等约束保持。本文件待用户人工转交，不代表已派发。保留 r1 与 Codex探针全部字节，新交付使用独占目录 `results/ae-cloud-multicall-20260912-r2/`。

## 必须修复的四组问题

1. **完整调用仍在时拒绝，实际运行门接上清单核对。** 补丁中未启用/未知profile/启用但混入未声明工具仍回退 `[0]`。真实源码切片→真实bridge已复现4→1并产生1次PATCH。将本实验保护放到服务端截断之前；启用失败、未知或不支持批次在任何工具副作用前停止并保留raw。shim当前只看profile字符串，与项目verify_letta_gate脱节；改为实际调用同一严格门，核真实模块与补丁文件，拒绝损坏/缺失清单。不要把“gate返回False然后继续截断”叫安全拒绝。不得通过开启parallel_tool_calls=true解决。
2. **生产入口、清单、审计统一。** 实际0.2 CLI PLAN不记录清单，re_code_files也缺ae_multicall.py。测试用的是自己造的另一种manifest。统一schema及path字段、固定文件集、基线/补丁/改后SHA、启用状态证据；plan/result必须由生产入口记录实际清单。正式manifest应可被同一checker读取；空patched_files/未知schema/缺文件不可获得完整输入VALID。采用同一生产清单生成器和CLI生成正例，禁止测试手补生产缺少的字段。部署路径可显式传入，不硬编码Mac临时目录；明确离线验证与实际服务加载验证不同。
3. **identity就是精确相等。** 删除按子串猜测的id_alias_table及“只要是另一个ID前缀就拒绝”的门，不把工具名混入ID集合。按逐次provider权威ID与实际approval/执行/回包/wire对应。`x1`与`call_x1_update`不能视为同一ID；`call_A`与`call_AB`作为两个合法原始ID必须通过。真实缺失/重复/截短/替换/乱序仍应拒绝。
4. **补真实回放和可达恢复链，不用合成替代声明。** 直接读取真实r2响应的ID/name/arguments字符串，第四项原category是“消费购物偏好”，不改成“购物偏好”；使用匹配初态与公开历史。覆盖真实审批恢复时is_approval_response=True的后续路径、多个tool_returns展开、完整filter及下一次wire。初次审批分支return不能证明回包恢复路径不可达。可在已有真实依赖的离线环境验证；缺依赖则交明确未完成项，不宣称只有付费云捕获未做。

## 修改与验收范围

复用原实现，修本次compat模块/shim/补丁、生成器与清单、RE driver/checker及两个RE入口、新增测试；优先最小变更。允许修本次自身测试设计错误，但不要继续改动无关旧测试来消除红灯。修复所需的辅助回归调整单列：恢复实质覆盖，采用封存代码隔离重放或独立真实辅助门回放，同时保留当前代码拒绝旧provenance的测试；不改capability checker、旧raw或既有辅助投影。

Codex已恢复 `ae_cloud_re_pair.py` / `ae_cloud_re_input_audit.py` 的准确改前字节与完整diff，见核验目录，不再以无完整基线替代差异审查。本轮所有修改前先保存字节与SHA。

验收优先重跑Codex现有探针：关闭/错误/mixed均零副作用停止；运行shim在缺清单/错SHA时不能启用；真实CLI生成清单记录，正式manifest可核；空清单/未知schema拒绝；合法前缀ID全链通过、错ID拒绝。再测真实四调用、同值连续更新、普通客户端工具批次、不可恢复故障前缀保留/后缀停止、正常error回包及原有RE顺序/来源/评分检查。不要只改预期码让当前错误行为变绿。

定向通过后跑一次受影响回归，若有共有模块变更再做一次全量。新捕获与报告不跳provenance门；所有fixture/源码切片/原生依赖连续路径逐项标明，不称为真实云运行。交付diff、前后SHA、清单、准确命令、实际日志、正反例报告和仍未覆盖项。

不联网下载、不SSH/上传/部署、不启动服务、不调用模型、不自动重试或派发新任务。完成离线交付后停止。
