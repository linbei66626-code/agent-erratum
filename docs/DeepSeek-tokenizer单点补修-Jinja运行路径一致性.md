# tokenizer 单点补修：Jinja运行路径与固定渲染路径一致性

2026-09-14。仅修计数入口的模板执行环境，不重开launcher/容量投影/端点验证，不跑大套件。项目AGENTS.md分工与离线边界继续有效。

## 已核进展

Codex复跑 tests/test_ae_target_tokenizer.py：9 passed。交付SHA256SUMS：29项一致。目标资产加载、模板分支选择、schema缓存key已有实现，保留。

## 新的直接证据

为核部署会自动使用的分支，Codex从本机pip缓存把Jinja2 3.1.6和MarkupSafe 3.0.3装入独立临时目录（没有修改项目venv）：

`/tmp/ae-tokenizer-jinja-review-20260914/`

运行时仅设置 `PYTHONPATH=/tmp/ae-tokenizer-jinja-review-20260914`，用项目 `.venv-vita/bin/python -B`，加载真实目标资产，逐条把原sealed请求messages/tools送入真实 `count_request_prompt`。无provider请求。

结果保存：`/tmp/ae-tokenizer-jinja-review-20260914/review.json`（只有计数/序列号/错误，不含私有正文）。

- 28条中22条抛 `jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'tool_calls'`。
- 剩余6条可以执行。其中sequence5得到7635，原固定renderer=7303，provider=7301；sequence11得到8084，固定renderer=7752，provider=7740。
- 当前 `render_with_template_runtime`使用StrictUndefined与Jinja默认tojson；固定renderer使用另一套JSON序列化。直接执行模板本身还需要匹配预期的环境/过滤器/可选字段语义，不能只安装Jinja后自动切换并声称等价。

## 唯一任务

使目标模板在Jinja可用/不可用两种受支持路径中，对相同请求具有核实的一致语义。

1. 处理模板合法访问缺失可选字段的语义，不篡改原wire消息、不要把可选字段缺失当坏输入；优先与官方模板运行环境保持一致。
2. 核对tojson对中文、HTML字符、键顺序/空格及工具参数的处理，与固定renderer和官方运行契约对齐。不能仅关闭StrictUndefined后就关闭问题。
3. 同一目标资产、同一28条sealed请求：两种renderer都实际执行并逐条比较渲染字节摘要/token数/各自provider残差；若无法保证等价，就显式选择单一已核运行路径，禁止按依赖是否偶然存在静默换算法。
4. generation prompt仍需如实说明所用假设：wire未包含assistant前缀不能证明provider不加前缀。不能为了对齐数字改原请求或用校准残差当未来上界。本轮无需猜测供应商内部实现。

## 最小验收与交付

只跑模板/计数相关定向用例：缺可选tool_calls、中文工具schema、两路径同请求对照、服务副本一致性；不重跑容量合同、生产proxy进程、stack/launcher等整组。

临时Jinja环境已经可用，不需要联网或安装模型框架。必要时项目明确小依赖方案，但不要默默改全局/项目环境。禁止SSH、部署、模型请求、修改RUN授权门，旧产物不覆盖。

新独占 `results/ae-target-tokenizer-jinja-fix-r1/`，短README、精确diff/SHA、28条对照及定向日志；先回答22条错误是否消除、运行环境变化是否仍会改变计数。完成后停止。
