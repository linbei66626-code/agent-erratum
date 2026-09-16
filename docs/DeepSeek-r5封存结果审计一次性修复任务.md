# DeepSeek 任务：一次性闭合 r5 封存结果审计

## 目标与停止点

只修**现有 r5 的离线审计**，把已经定位的记录接口问题一起闭合。不重新运行实验、不调用模型、不改评分结果。不要修一项就停下问是否继续；下面五项属于同一已授权任务。

最终交付应能对真实 r5 的18阶段运行完整审计链，明确区分：原严格协议是否通过、原生评分语义是否一致、是否仍有其他证据缺口。**目标不是强行得到 VALID。** 本轮不需要部署、SSH、联网、读真实密钥或重启服务。

## 先读的证据（不用重查旧轮次）

工作目录：`/Users/lrc/Documents/研究生资料/实验/agent-erratum`。

以下均相对此目录：

- `transfers/ae-deepseek-re-complete-20260916-r5/README.md`
- 同目录 `input-audit-offline-env-r1.json`、`judge-diagnostic-r2.json`、`judge-diagnostic-r3-echo-sensitivity.json`、`rubric-text-differences.json`
- 原始 run：同目录 `deployment/runs/ae-deepseek-re-live-20260916-r5/`
- 原始 journal：同目录 `deployment/runs/ae-deepseek-re-live-20260916-r5.private.jsonl`
- 同目录 `SHA256SUMS.remote`：原始7文件已与服务器校验一致。

`diagnostic-*.py` 仅帮助理解定位过程。其中有临时适配和取消单项断言，**不能直接充当正式实现或验收**。

已知：r5完成18阶段、258次生成均200；原审计的wire/工具映射/用户回复/记忆门已通过。诊断核实18项原生评分输入、98项辅助调用；15阶段其余judge门通过，R t7/R t10/E t10只在评分回复标准回显上不同，诊断取消该断言后其余judge门也通过。

## 修改范围

优先只改：

1. `ae_cloud_re_multiturn_input_audit.py`
2. `scripts/ae_01_cloud_re_multiturn_input_audit_sealed.py`
3. 对应定向测试与本轮独占交付目录。

不改驱动、NativeVita、模型/提示/R/E记忆/任务/预算、服务补丁、manifest、receipt、原严格CLI、运行代码身份列表。此轮只让审计正确读取既有证据；未来运行记录格式优化不在本轮范围。

开始修改前，保存并核实 r5 运行时审计模块的真实旧副本。r5 plan记录摘要是：

`51ab42fa69993f3ae5df3f4d608be12515527990c94e4314a70b2f7ae1bf124b`

还必须与真实result记录交叉核验；**不是** r4 的 `7d631dfb…`。沿用已有 `sealed_audit_copy` 机制，其余运行身份文件仍严格核验，当前审计者摘要单独记录。旧副本只做身份核验，不执行。

## 五项修复

### 1. 审计使用本run的Vita模型配置

封存入口在导入Vita之前显式使用本run的 `vita-models.json`，不依赖调用者遗留环境、不回退示例配置、不要求新API Key。记录配置来源/摘要；配置内容与本run声明核对，不能接受外部任意配置。若同进程已经载入另一配置，明确拒绝或用隔离子进程，不能仅改环境变量却复用旧模块。

验收须使用独立进程，禁止socket连接，确认整个重审零网络。原严格CLI文件保持原字节；README给出其所需环境命令即可。

### 2. 从真实快照取本阶段评分输入

当前驱动尝试读取 `snapshot.simulations`，真实NativeVita只有 `completed` 与 `last_evaluation`，所以r5缺 `judged_simulation_this_task`。

在审计内部建立只读解析：按本arm、本阶段的 `completed[].subtask_id` 唯一定位；与该阶段 `last_evaluation.simulation` 对照；completed、last_evaluation、record.judge 的reward相等；核对termination。缺失、重复、互相矛盾、错任务均拒绝。若显式 `judged_simulation_this_task` 存在，也必须核对一致，不能任意优先取一份掩盖冲突。不要搜索其他arm或之后阶段来“找一个能用的”。

报告记录证据的实际来源路径；不向原result补字段。

### 3. 校验真实Task/Message转换，而非字典直接相等

真实 `NativeVita.finish` 生成 `task_id = "U000828_subtask_" + subtask_id`；真实 `_messages` 逐条按原生类转换，设置turn_idx，缺timestamp时由原生默认值产生。

依据已核验源码与原生类检查该确定性转换。角色、内容、工具ID、参数、顺序等必须精确匹配；已有timestamp必须精确匹配；原transcript缺timestamp时，明确区分“运行时自动生成的元数据”，检查保存值形状及快照副本一致性，不能声称重新证明其实际墙钟时刻，也不能用重审当天时间代替。不能全局删timestamp/turn_idx再宽松比较。

### 4. 辅助调用按真实记录顺序绑定

真实native调用没有 `recorded_at`。使用已经验证的阶段执行顺序 + 阶段内native列表顺序，与代理剩余调用的捕获顺序逐位置一一绑定。

保留：累计native快照与每阶段slice的对应、角色/任务归属、请求正文及参数、原始响应、唯一消费、各阶段真实捕获时间范围、调用总数。不能按内容全局搜索匹配，不能伪造recorded_at；若记录确实带时间，应检验其一致性，不能无条件忽略矛盾。r5应匹配98条；交换/重复/删除、错阶段、错请求或响应必须拒绝。完全相同记录若无法区分物理身份，说明证据边界，不虚构识别能力。

### 5. 标准回显：保留严格结论，提供显式原生语义复核

r5有3阶段、6条评分回复把rubric里的括号举例省略了。原生evaluator按 `rubric_idx` 更新布尔值和justification，原标准文本来自初始states，不采用回复的rubric文本替换它。

**默认严格行为不变，不偷偷删除逐字回显门。** 新增显式选项，例如 `--judge-semantics native-by-id-v1`，用于事后原生语义复核：

- 完整标准仍来自固定dataset；发送的评分提示/窗口/携带状态必须按原生实现精确核验。
- rubric_idx完整、唯一、无多余项，回复字段类型正确；按原生更新规则逐窗口重算，最后标准文本仍为dataset原文，final检查和reward精确对应。
- 逐条记录所有回显差异（arm/task/window/rubric_idx/原文/回显），保留严格协议失败原因。
- 输出独立的 `native_semantics_checks_passed`（或同义字段）和明确的事后协议版本/范围。**严格回显失败时，原 `input_audit_passed` 仍为false，不输出无条件VALID，不改原result的scientific_result。** 新复核通过只表示原生评分链一致，不代表评分模型判断一定正确或R/E假说成立。
- 只有明确的回显差异可以收集后继续检查；不能吞掉任意AuditFailure。其他硬缺陷正常拒绝。

本任务授权实现并执行这个显式复核，不授权把新口径冒称原冻结协议。无需为这一实现再提问。

## 验收：真实封存优先，定向一次

1. 新入口独立进程、socket被禁止、真实r5输入，完整检查18阶段和98项辅助调用；不能mock前置门、judge、NativeVita记录或评分器，把fixture写成理想形状。
2. 严格语义仍拒绝上述回显差异；显式原生语义复核可遍历全部18阶段与后续闭合门，并如实保留严格失败。若有新的真实不一致，保留失败，不为达到预期而放宽。
3. 最小反例覆盖：错误/缺配置；错误或重复simulation归属；快照副本冲突；内容/工具ID/参数/turn_idx/已有timestamp篡改；辅助调用次序/归属/响应篡改；重复/未知rubric_idx、判定类型或最终reward篡改。可合并参数化，不为数量拆用例。
4. 沿用封存身份反例：旧副本缺失/错误、plan/result旧摘要不一致、其他运行文件改变均拒绝；旧报告不可覆盖。
5. 只跑新定向套件 + 直接受影响的封存重审/DeepSeek审计回归一次。不要全仓pytest、不要重新核验几十份无关旧交付、不要刷新stack。

### 本地路径差异怎么处理

本地不能改封存plan里的服务器路径或摘要来“让它过”。若完整公开入口因缺现场原manifest/receipt受阻，先清楚列出所缺**准确路径与期望摘要**；本地对真实18阶段运行剩余审计方法并声明未覆盖的前置范围。交付现场公开入口的精确离线命令，最后由Codex在服务器原路径执行完整链。不要新增通用路径映射/归档基础设施；不要以“代码写完”代替真实记录验收。

## 交付与现场动作

新独占目录建议 `results/ae-deepseek-r5-sealed-audit-repair-r1/`，已存在即拒绝覆盖。只需README、实际diff/必要旧副本、定向测试日志、真实记录复核报告、SHA256SUMS。不要重复拷贝数百MB journal，引用其路径与摘要。

README明确列出：修改文件、r5旧审计副本SHA、所有已验证/未验证范围、新语义字段和退出码、部署增量、完整重审命令。现场路径：

- run：`/root/agent-erratum/deployment/runs/ae-deepseek-re-live-20260916-r5`
- journal：上项后加 `.private.jsonl`
- config：`/root/agent-erratum/configs/ae-01__re-multiturn__deepseek-flash.serial-budget1024-candidate.json`
- dataset：`/root/agent-erratum/vendor/vita/tasks-full-a4553e1.json`
- Vita配置：run内 `vita-models.json`
- Python：`/root/agent-erratum/.venv-vita/bin/python`
- 输出：`/root/agent-erratum/deployment/ae-deepseek-re-launch-20260916-r5/` 下新文件，绝不覆盖旧报告。

**无需重建stack、重启服务、新回执或任何付费重跑。** 本轮五项及直接接线缺陷一起收尾；独立的新科学/数据缺陷才报告停止，不能悄悄改变实验定义。不要自行发起下一轮实验。
