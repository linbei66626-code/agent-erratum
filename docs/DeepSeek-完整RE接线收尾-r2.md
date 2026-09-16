# DeepSeek：完整 R/E 接线收尾 r2

继续上一份任务，授权范围不变，不需要再次询问是否允许接 CLI。只离线实现与验证，不联网/SSH/部署/读真实密钥/调用模型。原任务见 `docs/DeepSeek-官方API完整多轮RE最小接入.md`。

## Codex 已核到的真实缺口

r1 的 ae_deepseek_re_transport.py 是声明与校验/构造函数集合，不含真实发送实现。真实CLI、代理、多轮驱动、事后审计均未引用它；tests 直接调用这些函数。故r1只能称组件完成，不能称驱动接入完成。当前CLI没有 --exploratory-capacity-option，README示例是未来命令而非可执行交付。

服务no_trustworthy_count_basis确为阻断，但不只是补basis与manifest就能运行。r2请完成下面同一条真实链。

## 允许改生产入口；保持旧路径行为

“保留旧协议”指旧分支输入、拒绝规则和结果兼容，不是禁止修改生产文件。允许按需要小改真实CLI、ae_cloud_re_multiturn.py、ae_cloud_proxy.py/代理CLI、既有输入/传输审计、服务补丁/生成器/launcher及其定向测试。采用显式DeepSeek专属分支；不要把byte-only加入旧Qwen可信token计数列表，不伪造token值或verified。不要仅为保证旧文件零修改而新增无人调用的旁路。

旧封存results/transfers不变；工作树生产资产/manifest可以由现有生成器更新，交付前后差异与部署增量。不重建审计体系。

## 必须闭合的路径

1. 真CLI解析探索容量选项 → 真配置校验接受独立DeepSeek候选（旧0.3不放宽）→ 实际创建代理/Agent，身份与模式向服务传递。缺选项默认拒绝。
2. 实际proxy dispatch在规范化后、上游前使用DeepSeek profile与字节门。所有角色同出口、真实wire模型/非思考字段；串行min_interval=0，不sleep、不重试，整对预算256不重置。审计独立重算规范化差异，不能靠自报布尔值。
3. 两臂逐阶段实际输入应用时钟与通用属性说明，跨域not_applicable正确。R改写/E追加的记忆机制保留，不注入单t4正确状态快照。覆盖真实阶段构造而非直接调用stage_clarification就算接通。
4. Letta服务真实发送入口识别显式DeepSeek byte-only探索策略；请求前测量实际完整请求字节并守2097152上限，禁摘要/不丢消息。不得先调用Qwen主计数再进入分支；旧模型缺可信计数仍拒绝。声明的1M不是测量或保证，不用固定token阈值假装证明容量。
5. launcher将新策略参数明确送进真实子进程环境/配置并记身份，重建stack与manifest，bootstrap/receipt继续验证实际加载文件。现有Qwen tokenizer启动必填要求在DeepSeek专属策略下应如何处理也须接通，不留“服务能分支但launcher启动不了”。
6. 真实输入与传输事后审计接受这一新协议且校验实际证据、模型/模式/字节门/两臂干预/服务回执；缺失或错配拒绝。不调用独立audit_identity就宣称整条事后审计通过。

## 最小验收与停止标准

复用r1组件测试，新增少量链路区分测试：用真实CLI解析、配置、代理dispatch与审计入口，外部HTTP可离线替身；两臂至少t4→t5与一次非delivery构造；显式探索正常分支与缺声明/超字节/错身份拒绝分支；服务真实发送方法/launcher-bootstrap政策选择实际执行，允许替换数据库和进程依赖，不mock被测门。断言DeepSeek零Qwen计数、零summarizer；超预算零上游；正常脚本化链可到真实事后审计。

只跑相关套件，不跑全仓、不重验几十份交付。不以mock证明端点上线或模型成功。若发现独立于当前接线的新科学问题，记录不扩做。

r2完成标准：离线正常分支从真实CLI到代理/服务策略/阶段输入/审计无已知自相矛盾；现场剩余仅网络、凭据、服务启动回执、供应商参数接受性及真实运行。不能再把尚未接入的函数标为运行路径已实现。

## 交付

新独占 `results/ae-deepseek-re-multiturn-integration-r2/`，已存在即拒绝。短README+关键diff+链路日志+SHA，列确切部署增量/是否重启/新receipt以及真实可解析的plan/preflight/run命令。未支持的CLI选项不要放成现成命令。候选保持探索性标注，现场由Codex核对后单次执行，交付后停止。
