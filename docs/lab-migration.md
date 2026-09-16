# AE-01：实验室迁移入口（2026-09-11）

## 最新执行状态（本节优先于下方准备记录）

**2026-09-13：校园网重连后的新配对，R完成、E虚构订单号支付失败，整对INVALID。** 用户确认校园网未登录并重连；无密钥HTTPS收到401，实际RUN前短时限网络检查通过。新服务PID28285/新receipt已核，真实数据集PREFLIGHT_PASS。30次推理全部HTTP200；R改写p007为7分糖并完成正确已支付订单，原生诊断评分3/3。E追加3条修正且采用7分糖，但未调用create_delivery_order就声称订单已创建，后以自行生成的O202406231430001调用pay_delivery_order；环境订单为空，报Order not found，当前异常规则停止整对，E未评分。provider与Letta最后调用ID/名称/参数精确一致；本轮全部单调用，无同批多调用新云证据。传输捕获通过，完整输入审计因execution_not_complete为INVALID，不据此判R优于E。41成员证据回收、40内部SHA通过；60保护文件不变，服务全部停止。见[本次结果与轨迹诊断](</Users/lrc/Documents/研究生资料/实验/agent-erratum/transfers/lab-cloud-re-multicall-run-20260913-r2/README.md>)。每次实验室实验前/久置恢复先检查服务器出网的用户要求已写入项目AGENTS及本次运行脚本；未改生产错误处理、未自动重跑。以下为历史。

**2026-09-13：新0.2单t4配对在云连接阶段停止，服务已关闭。** 用户继续授权后，复核真实服务receipt和41代码/配置，真实数据集PREFLIGHT_PASS；只尝试一次RUN。第一笔GET /v1/models的外部443连接SYN-SENT，上游未返回HTTP，代理报upstream_io_failure_no_retry并返回502。chat请求0、arms为空，未创建/执行两臂；runner及完整输入审计均INVALID（transport_capture_check_failed）。因此不是多调用/记忆更新失败的新证据。代理/Letta/数据库已停止，8000/8283/5432无监听；51受保护文件不变，31成员证据回收并核30内部SHA。见[本次尝试与网络断点](</Users/lrc/Documents/研究生资料/实验/agent-erratum/transfers/lab-cloud-re-multicall-run-20260913-r1/README.md>)。下一步恢复服务器出网，校园网认证是否到期尚未证实；恢复后须新服务receipt及新目录，未自动重跑。以下为历史。

**2026-09-13：实际 Letta 服务已启动，真实进程 receipt 与生产前置校验通过。** 用户恢复SSH后，两文件补修按旧摘要备份部署，39依赖与固定manifest不变；服务器43项定向测试27.426秒全过无skip。Letta PID27997，服务器127.0.0.1:8283健康0.16.8/ok，数据库5432保持运行，代理未启动。真实进程argv/五项环境/源码解析路径与SHA/receipt PID均已核，共享校验器及生产require_receipt=True接受；带receipt的禁网CLI PLAN成功。首次健康等待超时exit2保留，同一PID随后健康通过，未重启；NLTK下载返回false而已有本地英文表加载通过，上游正常完成启动。33成员证据回收并核32项内部SHA，见[实际服务验收记录](</Users/lrc/Documents/研究生资料/实验/agent-erratum/transfers/ae-multicall-source-gate-deploy-20260913-r1/README.md>)。没有模型请求/新R/E RUN；源码解析与receipt通过不等于四调用真实wire或科学结果通过，旧R/E仍INVALID。以下为历史。

**2026-09-13：源码门补修已完成本地定向核验，待恢复 SSH 后部署并启动。** 交付17项SHA及两当前文件摘要通过；真实源码门/显式补丁绑定接线已读，部署31项+bootstrap12项独立复跑43项全过、0skip。核验禁外连/监听，允许本机选端口bind；首次过严bind禁令产生的失败日志保留，未改生产/测试求通过。另封两文件增量包及39项不变依赖摘要，旧包保留。SSH实测Permission denied (keyboard-interactive)，旧control已失效；尚未上传/部署/启动。见[核验与待部署增量](</Users/lrc/Documents/研究生资料/实验/agent-erratum/transfers/ae-multicall-source-gate-deploy-20260913-r1/README.md>)。继续沿用实际服务启动授权，不执行模型RUN；目标机当前服务状态未重新核准。以下为历史。

**2026-09-13：实际 Letta 启动在源码门被拒绝，服务已收尾。** 用户授权启动后，数据库启动成功；Letta 的 source_check 无条件拒绝 tracked dirty，将已核 agent/message 补丁也拒绝，exit2，尚未创建服务进程或 receipt。HEAD/manifest/三补丁文件 SHA 均与部署记录一致；已有启动接线测试 mock 此门，离线通过未覆盖组合路径。数据库随后停止，8000/8283/5432 无监听；未改生产/测试、未运行模型。见 [实际启动记录](</Users/lrc/Documents/研究生资料/实验/agent-erratum/transfers/lab-multicall-service-20260913-r1/README.md>) 与 [最小补修任务](</Users/lrc/Documents/研究生资料/实验/agent-erratum/docs/DeepSeek-多调用启动器源码门补修任务.md>)（已备未派发）。下一步只补显式 opt-in 的严格源码门兼容，实际 bootstrap/健康/receipt 仍待验证。以下为历史。

**2026-09-13：多调用累计更新已部署，实验室107+99项禁网与真实数据PLAN/PREFLIGHT通过。** 21项目标按旧摘要备份安装、Letta只改两文件加shim，完整原版保留、manifest现场生成；57项历史记录及依赖/数据保持，42成员证据已回收核SHA。服务未启动、无新模型调用或实际实例receipt。见[部署收尾与下一步](../transfers/lab-multicall-deploy-20260913-r1/README.md)。以下为历史准备与运行记录。

**2026-09-13：多调用r5本地离线验收通过，累计部署候选包已准备；待SSH认证与两处测试路径传参适配。** 21文件候选与6文件只读测试证据包逐成员SHA通过，20项既有依赖待现场核；未上传/部署/启动服务/模型调用。见[当前候选包与后续流程](../transfers/ae-multicall-deploy-20260913-r1/README.md)。以下为历史执行记录，旧真实R/E仍INVALID。

**2026-09-12：校园认证后单t4 R/E r2两臂任务完成，但完整输入审计INVALID，服务已停。** E原始响应4个工具调用被固定Letta截为1个，tool_call_count_changed正确拒绝；两臂正确模拟订单及debug 3/3仅作诊断。数据集声明路径已核，封存证据已回收，生产/旧raw不变。见[本次运行与截断定位](../transfers/lab-cloud-re-pair-run-20260912-r2/README.md)；下一步离线处理多调用边界，不自动新RUN。下文为历史。

**2026-09-12：首次真实R/E尝试在上游目录连接阶段INVALID，未进入两臂，服务已停。** 唯一GET /v1/models发生上游I/O失败，代理返回502；无chat、Agent创建或任务行为，输入审计拒绝。已回收核验全部选定证据，代码/旧r4不变。见[本次运行与诊断记录](../transfers/lab-cloud-re-pair-run-20260912-r1/README.md)。下一步先排查个人容器出网连通性，尚无R/E科学结果。下文为历史。

**2026-09-12：单t4云R/E六文件已部署，实验室54项禁网测试与真实数据PREFLIGHT通过。** 仅新增双臂模块/CLI/配置/测试，既有依赖与旧r4记录前后SHA不变；两臂同初态、26条历史、各19工具，仅预览。服务未启动、无模型调用或真实R/E RUN。见[部署与回收证据](../transfers/lab-cloud-re-pair-20260912-r1/README.md)；下一步以知识库项目状态为准。下文为历史记录，其中r4输入审计已在后续完整路径核验闭合。

**2026-09-12 r4：单t4任务与原生评分完成，完整输入审计待修，服务已停。** 当前状态与下一步以知识库项目状态为准；本次20个chat均HTTP200，订单oracle满足，debug judge 3/3。辅助消息审计因内部timestamp与wire表示差异未通过，固定Vita真实格式化已做7/7禁网诊断。没有修改生产代码或自动重跑。见[本次证据及命令](../transfers/lab-cloud-t4-20260912-r4/README.md)。下文保留历史。

**2026-09-12：节流已部署，真实单t4 r3因上游空回复INVALID，服务已停止。** 服务器149项离线测试及预检通过后运行一次；2个chat均HTTP200、发送间隔65秒，无429，但第二次供应商原始响应为`content=""`、`finish_reason=stop`且无工具调用，桥接停止。尚未下单或评分；传输检查通过、完整输入审计因未完成而拒绝。35文件已回收逐字节核包；下一步隔离空回复来源，不自动重跑。详见[本次命令与原始记录](../transfers/lab-cloud-t4-20260912-r3/README.md)。下文为历史。

**2026-09-12：统一出口节流已完成本地修复和149项离线复核，尚未部署。** DeepSeek编码，Codex检查并复跑（2.320秒，无skip）；Agent/用户模拟器/judge共用65秒发送间隔，首次亦冷却，仅新配置启用。driver等待上限相应为900秒，代理上游I/O仍180秒；429后继续停止，不自动重试。模型/任务/评分/旧raw不变，旧r2仍INVALID。见[本地验证记录](../results/ae-cloud-pacing-20260912-r2/codex-verification/verification.md)和[节流机制](cloud-pacing.md)。下一步最小部署、服务器离线核验，再新建单t4；下文为历史。

**2026-09-12：云单t4 r2在用户模拟器请求处触发TPM限流，INVALID，服务已停止。** 上次商品错误已能回传，模型随后搜索并在模拟环境中创建/支付正确订单，私有oracle满足但仅诊断；原生judge未运行、完整输入审计未通过，不能称完整任务PASS。15次chat成功＋1次429，未重试。25项证据已回传核SHA，见[本次命令与结果](../transfers/lab-cloud-t4-20260912-r2/README.md)。下一步统一出口节流方案，不先换模型或改任务。下文为历史。

**2026-09-12：修复已部署到实验室，172项离线复核全部通过（9.810秒，无skip）。** 7文件SHA一致，5旧文件已备份；31项非目标文件/配置/旧任务记录前后不变。未启动服务、调用模型或读取密钥。日志及备份已下载并核14项SHA，见[本次部署记录](../transfers/lab-tool-errors-20260912-r1/README.md)。下一步新目录单t4试跑与输入审计；下文“未部署”为历史时点。

**2026-09-12：工具错误与审计wire修复本地验证通过，未部署。** DeepSeek实现，Codex联合复跑172项离线测试通过（2.583秒，无skip），真实旧run的工具字段局部检查通过，混合wire字段篡改仍拒绝。修复范围及限制见[工具错误兼容](tool-error-compat.md)；验证收据`results/ae-tool-errors-20260912-r2/codex-verification/verification.json`。代码三文件`ae_adapter.py`、`ae_task_run.py`、`ae_cloud_input_audit.py`及相关测试/文档改变；28项非目标文件与16份旧run记录未变。下一步清单式最小部署，服务器旧代码先备份，离线复核后才能新run；不重用旧输出、不改模型/prompt/oracle、不直接R/E。下文运行与部署为历史时点。

**2026-09-12：一次真实云单t4已运行，INVALID并已停服。** 新run为`deployment/runs/ae-capability-cloud-t4-20260912-r1/`。模型先调用`get_user_all_orders`得到空结果，再生成输入中不存在的`P1001`；商品详情工具报`ValueError`，桥接层停止。两次云模型响应传输结构检查无issues，但完整输入审计为`execution_not_complete`，不是科学结果。未重试、未进t5/R/E，代理关闭且8000/8283/5432无监听。16个原始/操作/审计文件已回收至`transfers/lab-cloud-t4-20260912-r1/`，压缩包SHA`fad68396955c96b0e77530132e2c04428455a852a60cc05c9d8340f1add17f7c`。下一步核对只读工具错误回传与原生Vita的差异；需编码则交DeepSeek，本轮未修改代码/配置。以下离线准备记录为历史。

**2026-09-12：最小更新已部署并完成实验室离线复核。** 8 payload独占新建、25既有依赖SHA一致，上游固定源码tracked清洁；Python3.11.16下72项测试全过（7.325秒，无skip），代理PLAN_ONLY及无socket的t4 PREFLIGHT_PASS。目标商品/7分糖/工作地址/19工具可用，t5只预览未执行。无模型/API调用、未启动数据库/Letta/代理。记录位于`deployment/lab-cloud-audit-20260912-r1/`，10文件已回传本地`transfers/lab-cloud-audit-20260912-r1/`并逐项核SHA；测试日志SHA`4db51f9be73833d8ce6956d4f0bef4d8e96547272baf3d2cdaa9b3e9ea314663`。下一步才是单t4真实能力探针＋事后输入审计，不直接R/E。下文等待登录/部署为历史状态。

**2026-09-12：更新包已备好，等待SSH重新认证。** 本地输入/渲染/运行准备72项通过；测试现支持 `AE_LETTA_SOURCE=/root/agent-erratum/vendor/letta-v1`，显式错误路径报错，不可接受skip。包 `transfers/ae-cloud-audit-update-20260912-r1.tar.gz`（55,525 bytes，SHA `3e8345e0829d47b51d3c9505ea902be740bc2a52f5807a21824e41fe912154fc`）；同名前缀manifest含8个payload、25个既有依赖及源码SHA。尚未上传/部署，60014可达但旧认证已失效。先恢复用户登录，核现有依赖和目标文件，做离线复核与无模型preflight；不启动t4。回收目录拟用本地 `transfers/lab-cloud-audit-20260912-r1/`，不存在才创建，不覆盖旧记录。

**2026-09-12：完成事件漏检已修复，本地定向回归53项通过，尚未部署。** 生产仅改 `native_capture`；驱动、CLI、framing测试未改。证据 `results/ae-cloud-events-20260912-r1/readiness-receipt.json`，知识库日志“完成事件漏检修复与定向回归通过”。下一步准备最小更新包、实验室离线复核，再推进单t4；不是完整服务链或任务能力已经通过。下文保留历史状态。

**最新：反例构造修复，当前剩一项复现的完成事件漏检（2026-09-11）。** Codex 联合复跑输入审计28项＋framing18项：45通过、1失败；生产检查器SHA未变。重复末尾 `task_complete` 仍被接受，下一步最小修此事件门；整套审计未验收，未部署或运行真实 t4。证据 `results/ae-cloud-mutations-20260911-r1/readiness-receipt.json`；知识库日志“反例构造修复与完成事件漏检”。

**最新：真实 Letta 渲染专项通过，完整输入检查仍待修（2026-09-11）。** DeepSeek 修两个 framing 函数及相关常量；Codex 复跑 `test_ae_cloud_framing` 的 18 项全部通过并核最终 SHA。期待输出来自固定源码原函数离线执行，非完整服务集成；参见 `results/ae-cloud-framing-20260911-r2/readiness-receipt.json`。旧反例构造和事件门仍未验收，下段初稿失败记录是历史；未部署或运行真实 t4。

**最新：云单 t4 输入检查器初稿未验收（2026-09-11）。** DeepSeek 新增实现、CLI、测试及 [说明](cloud-input-audit.md)，没有部署或真实实验调用。Codex 复跑 26 项：`failures=18, errors=3`，exit 1；4 份交付文件 SHA 与 r1 收据一致。手抄 renderer 与固定 Letta 源码不一致且正例同源；反例构造及部分事件门仍须修复。不得把合成正例的 VALID 当成检查器验收。下一步只修这些缺口，再考虑云 t4；不启动服务、不改变原协议。完整定位见知识库 AE-01 日志“云输入检查器初稿未验收”。

**最新：云 t4 运行候选已由 DeepSeek 实现、Codex 复核（2026-09-11），仅本地离线准备。** Harness 已恢复认证并完成真实委派；新增配置、测试、[运行准备说明](cloud-capability-readiness.md)。最终 r3 的 19 项新测试、1 项既有 NativeVita 定向测试通过，Codex 复跑 19 项并核 3 文件／2 日志 SHA；收据 `results/ae-cloud-readiness-20260911-r3/readiness-receipt.json`。没有部署候选、启动服务或调用实验 API。256 请求是候选安全 cap，不是总调用上界；缺少云单任务完整输入审计，下一步先补这一层再推进真实 t4。下方 Harness 受阻记载是已解决的历史时点。

**最新：SiliconFlow 小合成连接探针 PASS（2026-09-11），本轮服务已停止。** 用户已录入 key，仅核文件权限 600／父目录 700，不显示或回传密钥。独立 PostgreSQL 数据库初始化、迁移至 `1c28e167b74f` 成功；首次 Letta 启动因缺 `punkt_tab` 失败，服务器下载超时后从本机获取 NLTK 官方资源、核 SHA 再传入，未改启动门或上游代码。资源 SHA256：`e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106`。Letta 0.16.8 健康检查随后通过。

`Qwen/Qwen3-30B-A3B-Instruct-2507` 完成 2 次推理、1 次 `read_probe` 工具调用及 nonce 回传，连接结果 PASS；代理原始记录检查通过，供应商报告合计输入 1357／输出 50 tokens。仅证明合成连接，`task_success`／`scientific_result` 仍为 null；未跑 t4、R/E 或验证 KV 复用。5 个连接原始文件已回传并逐项核 SHA，另保留本地传输审计与 9 份部署结果，位于 `transfers/lab-cloud-connection-20260911-r1/`。代理、Letta、数据库均停止，数据库与失败记录保留；恢复用 `start-db`，不要再 `prepare-db`。

下一步是准备单个 t4 正确当前状态能力探针的云传输预算及输入检查，不直接进入 R/E。本轮只执行现有脚本与补依赖，没有编码；DeepSeek Harness 被内置浏览器报 `ERR_BLOCKED_BY_CLIENT`，未成功委派，不能声称代码由 DeepSeek 完成。需要编码时先恢复该委派通道或请用户决定替代方式。

以下保留前序准备记录，其中等待密钥／未建库是历史状态。

**最新：云任务接口离线适配完成，待用户录入密钥（2026-09-11）**。19 文件更新已部署，8 个旧版本先备份；实验室 227 项测试通过（25.470 秒），固定 Letta 实际生成的三种请求通过 renderer 检查，云 t4 静态 preflight 通过。不是数据库／Letta 在线链已验证，也没有 API 请求。18 个检查／备份文件已回传 `transfers/lab-cloud-task-20260911-r1/` 并核 SHA。实现、兼容性边界及录入命令见 [云任务适配](cloud-task-compat.md)。下一步用户只需在服务器运行 `scripts/ae_01_set_cloud_key.py`，密钥仅保存在 `deployment/private/siliconflow.key`；再接通本地服务、做小合成连接探针，不直接启动 t4 或 R/E。

**云传输原型已部署并在实验室离线复核（2026-09-11）**：更新包 SHA、8 个 payload 文件及已有依赖哈希通过，Linux Python 3.11.16 下 216 项测试全部通过（14.914 秒，无跳过）。PLAN_ONLY 未读密钥／发请求；完整 Letta/Vita API 任务路径仍未接通。检查记录在服务器 `deployment/lab-cloud-transport-20260911-r1/`，已回传本地同名 `transfers/` 子目录，5 个记录文件逐项 SHA 一致。测试后 payload 与固定上游 tracked 状态复核通过。范围和待适配参数见 [云传输原型](cloud-transport.md)；该页及 r1 包保留本地实现时的状态快照，部署进度以本节为准。

用户亲自完成首次 SSH 登录后，助手通过该临时会话进入 `root@lrc`，没有读取密码、新增端口或修改共享宿主机。

- 已创建 `/root/ae-incoming-20260911/` 与 `/root/agent-erratum/`，传入两份包；远端 SHA 相符，解包后 bootstrap 的 71 文件、4B 证据的 48 文件逐项字节一致。机器收据：`deployment/lab-install-20260911-r1/transfer-receipt.json`。
- 固定 Letta／Vita 源码与全数据已恢复到 `vendor/letta-v1`、`vendor/vita/source`、`vendor/vita/tasks-full-a4553e1.json`。两源码 HEAD 与原记录一致、tracked 无改动，数据 SHA 相同。Mac 归档保留的 UID 导致初次 Git ownership 检查拒绝；只将新解包项目的所有者设为 root 后复核通过，未放宽全局 safe.directory。
- 1326 个已核 magic、未跟踪的 AppleDouble 副文件由现有脚本移动到 `deployment/quarantine/20260911T104738Z-ed4bb2/`，附清单、可恢复；未删除上游源码或原始包。
- 项目专用 uv 0.12.13 与 CPython 3.11.16 已安装在 `.tools/`；系统 `/usr/bin/python3` 仍是 3.10.12。Letta 0.16.8 已按原 `uv.lock` 安装到 `.venv-letta`（server/postgres/redis extras，不含 dev extra），258 包依赖检查及关键依赖导入通过。Vita 独立安装到 `.venv-vita`，77 包依赖检查通过；版本以旧 Vita freeze 为约束，仅排除旧机器的 Vita 源目录引用，固定源在新路径重新安装。完整新 freeze 保留，不声称两个环境逐包相同。
- 原 Ubuntu HTTP 源连接失败；单次安装使用项目内 `scripts/deployment/ae-lab-ubuntu-https.sources` 与已有 PGDG sources，不覆盖系统源、不做整体升级。PGDG 签名 key 从官方 HTTPS 获取。PostgreSQL 15.19、开发头文件和 pgvector 0.8.1 已安装；后者用固定源码 `make -j2` 编译（原 Makefile 含 `-march=native`，不将此编译产物当作跨主机通用包）。`ae-lab-no-default-cluster.conf` 已放入容器 `/etc/postgresql-common/createcluster.d/`，禁止包安装自动建默认库；`pg_lsclusters` 为空，未初始化实验数据库。
- 新机 t4–t5 静态预检通过（2 tasks／59 history records），记录 `deployment/lab-install-20260911-r1/offline-preflight-t4-t5.json`。没有 tokenizer 测量、模型调用或 runtime 科学结论。
- **189 项离线测试全部通过，12.933 秒，无跳过**。真实 Vita 集成测试使用固定数据和原生工具／用户／评分组件，但模型回复全为 fixture；测试中的 VALID 与评分不是新模型结果。日志 `deployment/lab-install-20260911-r1/offline-tests.log`，SHA `631911eb828cd3a38e7fb00f89c5b5e1d6198df58cd073573b8d60924d5135af`；环境收据 `environment-receipt.json`。源码 tracked 检查仍为空。
- Vita 首次安装在大 wheel 下载阶段较慢，礼貌中断该下载进程、保留缓存后，用 Mac 从官方 PyPI 下载 NumPy 2.4.6／SciPy 1.17.1 Linux CPython 3.11 wheel，Mac 与新机均对 PyPI SHA 验证一致，再沿用原版本约束完成安装。未改依赖版本求通过；两个 wheel 保留在新机收件目录，哈希在环境收据。
- 收尾根盘 25G／已用12G／可用13G（49%）。未启动 Letta、模型代理或实验数据库，没有 API key／付费请求。安装记录已回传本地 `transfers/lab-install-20260911-r1/`；下一步是云 API 接口适配及配置密钥，再单独接通服务和做小探针，不能直接当作 R/E 可运行。

工具安装参考：[uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)、[PostgreSQL Ubuntu 官方说明](https://www.postgresql.org/download/linux/ubuntu/)。下面保留迁移前的盘点与授权经过；其中“尚未上传／安装”属于该时点，不代表最新状态。

## 实验室实际运行前先检查出网（2026-09-13 用户要求）

校园网认证会过期。每次正式实验前或长时间暂停后恢复，先从服务器做不带API Key的短时限HTTPS检查，保存时间与状态码/错误；401/403说明HTTPS可达，不能当作鉴权或推理成功。SSH登录只说明能进入服务器。无响应/连接失败时先停止前置步骤，由用户交互重连校园网，再检查；不要用付费RUN充当网络探针，不自动重试模型，也不保存校园网密码。

本次操作脚本的已执行样例见 `transfers/lab-cloud-re-multicall-run-20260913-r2/run-on-lab.sh` 的网络前置部分（该完整脚本含已执行RUN，**不得整段重跑**）。该检查使用curl连接5秒、总计15秒上限；随后才加载代理Key/运行任务。恢复服务须用新进程receipt，实验用新目录，保留旧失败结果。

## 本轮决定和边界

用户准备改在实验室运行任务环境，并从 SiliconFlow 选 API 模型。本轮只核资源、准备迁移资料；没有改 R/E 定义、改旧模型配置或调用付费模型。旧 8B／4B 的 INVALID 记录继续保留。

目标是实验室已有的个人 `lrc` 容器，不是共享宿主机。以 [校园服务器交接](</Users/lrc/Documents/Obsidian Vault/计算机学习/Linux与Shell/校园服务器操作与学习交接.md>) 为入口；不要重建 LXD、GPU 设备或端口代理。2026-09-11 本轮已在 Windows App 的现有远程桌面内只读核实 `hostname=lrc`、`whoami=root`、`pwd=/root`；Python 3.10.12，pip 不存在，PATH 未找到 conda/uv，git/curl 在 `/usr/bin`。`df` 显示容器根盘 25G、已用 11G、可用 14G，不据此推定额外共享资源配额。暂定目标 `/root/agent-erratum` 和收件目录 `/root/ae-incoming-20260911` 均不存在，尚未创建或上传。

容器能够收到 PyPI 的 HTTPS 响应头；SiliconFlow `/v1/models` 的无密钥 HEAD 返回 HTTP/2 404，只证明域名/TLS/HTTP 可达，不是鉴权或推理接通。第一次终端输入的 `:` 和大写 `I` 被远程键盘映射丢失，那两条报 `Could not resolve host: https`，不当作网络故障证据；改用经屏幕核对的 `--proto-default https --head` 命令完成检查。原版 Letta 的 Python 要求为 `>=3.11,<3.14`，因此后续应建独立 Python 3.11 环境，不改系统 Python 3.10。尚未安装任何依赖。

## 两个需要携带的包

1. 本地 `transfers/ae-lab-bootstrap-20260911-r1.tar.gz`：当前代码／配置／脚本／测试／文档、固定 Vita 源码与全数据包、固定 Letta 源码包、pgvector 源码、已有两份原始证据及离线检查。是恢复材料，不是已经接通 SiliconFlow 的可直接运行环境。
2. `results/ae-capability4b-evidence-20260911-r1.tar.gz`：最新 4B 原始证据，**用户已下载并单独解压，本地验收通过**。SHA 与远端封存值一致：`2ca75f0dcb1b4791c2fd138c03613f1584f5966f2fec3de9ad5ae7a3ba0f6ad9`，1,528,130 bytes；48 普通文件逐项字节一致，9,157,820 解包字节，无缺失／额外文件或不安全链接。验收收据 `transfers/ae-capability4b-local-receipt-20260911-r1.json`。旧服务器原件未删，INVALID 不变。

用户下载后，本轮检查旧实例页面已是“已停止”、开关关闭，释放时间 `2026-09-18 16:52:12`。助手未再切开关，不冒称由本次助手关机。没有释放实例、删除数据或启动模型。此前 CPU 启动只为取回文件。

bootstrap r1 是上一轮封存快照，里面文档仍保留当时“4B 包待下载”文字；原包和 SHA 不覆盖。当前迁移状态以本页及验收收据为准，最新 4B 包单独随行。

无需迁移模型权重、vLLM wheel、Mac/Linux venv、缓存、旧服务 PID 或旧数据库凭据。旧数据库本轮未备份；新机将创建新运行用专用数据库，不能声称能够恢复原 Agent 数据库会话。固定上游包自带的旧 AppleDouble 副文件仍需用已有隔离脚本处理，不改上游源码。

上传和解包：两个包先放容器的独立收件目录（如 `/root/ae-incoming-20260911/`），核 SHA 后，bootstrap 解到一个**全新**项目目录；4B 包只解到项目 `results/ae-capability4b-evidence-20260911-r1/`。当前没有上传或解包到实验室。旧包里的路径是历史 provenance，不应批量改写。

2026-09-11 传输入口检查：临时退出容器，在已核身份 bjut@bjut-PowerEdge-T640 下只读列出 `/home/bjut/thinclient_drives/`，仅有 `.clipboard`，无 Mac 共享文件夹。随后已返回 root@lrc。尚未创建共享映射、上传、安装或登录 SSH。建议经用户确认后仅用现有 `172.21.4.190:60014` SSH/SFTP 传文件，日常操作仍留 RDP；首次凭据由用户自行输入，不在对话或日志保存密码。不因传输不便新增端口或扩大共享目录。

随后用户已明确批准临时通过已有 SSH 通道完成传输和部署。本机探测 60014 返回 OpenSSH 服务及 ED25519 公钥；这是网络观测，不冒称已完成独立主机身份验证。当前计算机控制工具禁止操作 Mac Terminal，故交用户亲自在终端首次登录，助手不绕过应用限制。为本次连接创建本地私有临时目录 `/tmp/ae-lab-ssh.Mgu1in`，拟用其中 `control` 作为 SSH 临时会话 socket（不存密码、不转发 agent、结束后关闭）。此时尚未登录、传包、创建容器目录或安装环境。

## API 候选（不是能力验证通过）

建议先试 `Qwen/Qwen3-30B-A3B-Instruct-2507`。本轮在 [SiliconFlow 登录态目录](https://cloud.siliconflow.cn/me/models?types=chat) 现场核到：在线推理、256K 上下文、工具调用、仅非思考模式；输入 ¥0.000700/K tokens（¥0.70/M），输出 ¥0.002800/K（¥2.80/M）。当前账户 L0：最高 RPM 1,000、TPM 40,000。**256K 是服务窗口，不等于当前账户能无条件发送任意长度请求**；长轨迹超过 TPM 的请求准入规则尚需核实，先单并发并检查 429，不能当作模型失败。

选择理由是文本工具任务、较长窗口及无需额外处理思考状态；不是基准成绩保证，也不是“30B 必然成功”。先完成无矛盾历史的单任务能力入口，再决定是否进入 R/E。API 定义与参数参考 [官方 Chat Completions](https://api-docs.siliconflow.cn/docs/api/chat-completions-post)。未创建／读取密钥、充值或发模型请求。

## 后续接入的最小工程范围

- 保留任务 → 本地审计代理的路由，由代理单独持有私密 API key、只向指定 HTTPS 域名转发；密钥不进配置、raw、压缩包。
- 现有 `ae_model_proxy.py` 仅认 loopback vLLM、必须 `/tokenize` 并与 usage 一致。另立云 API 审计配置，保留请求／响应／usage／trace-id／结束原因；不能伪造服务端 token IDs 或把本地估算当精确分词。
- 检查模型白名单、Letta provider 的 `max_model_len` 元数据、`max_completion_tokens`/`max_tokens` 和工具返回 ID 的兼容。所有旧配置保留；不改 Letta/Vita 上游。
- Letta 与 Vita 继续独立 Python 环境（uvicorn 版本要求不同）；固定 Letta 带 uv.lock，已装依赖记录在旧任务证据包 `deployment-logs/{letta,vita}-freeze-20260911-r1.txt`。不要直接重放 freeze 中旧本地源码路径。
- API 只用于行为实验；本轮不能测或宣称真实 KV 前缀复用、缓存命中或 KV 加速。

下一步：传入两个包 → 在 lrc 建隔离依赖环境 → 接口适配与小探针。资料本地保全及容器初检已完成；迁移资料齐全、接口连通和科学有效是三件不同的事。API key 尚未配置，不发送付费推理请求。


## 2026-09-13 支付前置错误修复已部署

累计四文件已安装，服务器禁外连62项全过、0skip，38依赖和112保护文件不变；服务未启动、无模型请求。备份及证据见[部署记录](../transfers/ae-payment-deploy-20260913-r1/README.md)。下一步实际服务启动与新receipt核对；旧云配对仍INVALID。


## 2026-09-13 支付修复后的服务测试通过

实际Letta PID29422健康，当前receipt及生产前置校验通过；真实数据集PLAN/PREFLIGHT通过，未调用模型。8283/5432保持运行，8000未启。见[服务测试记录](../transfers/ae-payment-service-test-20260913-r1/README.md)。该进程状态为本次检查快照，实际RUN前须重核。


## 2026-09-13 支付修复后配对由用户中止

R臂重复门店/商品查询各8次，未建单，E未启动。用户要求停止后服务全部关闭，20次推理已返回200；缺最终result，审计INVALID，保留为中止诊断轨迹。见[本次证据](../transfers/lab-cloud-re-payment-run-20260913-r1/README.md)。不自动重跑。
