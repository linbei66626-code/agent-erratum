# AE-01：工具错误兼容边界（2026-09-12）

本轮修复云单 t4 INVALID 的一处真实成因，实际因果顺序是：查询订单的 READ 工具对空订单返回空字符串
（上游行为，不改）→ 模型在无依据下生成不存在的商品号 `P1001` → `get_delivery_product_info` 抛
`ValueError("P1001 not found")` → 桥接层把任何工具异常都升级为 `BridgeBlocked`，任务随即中止，
因此**没有**任何让模型看到错误并自我纠正的后续轮次；中止不是此前臆造的原因，而是让该臆造无法被纠正的原因。
修复只把**已核验的原生只读查找分支**回给模型，其余一律保持阻断。

## 允许回给模型的错误（精确范围）

条件全部满足才可恢复，任一不满足即阻断：

1. 绑定由原生元数据判定为 READ：`ToolKitBase.tool_type(name)` 返回 `ToolType.READ`
   （来自 `@is_tool` 写入的 `__tool_type__`）。不使用 `get_` 前缀等命名猜测；缺失元数据一律按非只读。
2. 命中**已登记并离线核验**的查找分支：目前仅
   `vita/domains/delivery/tools.py` 的 `DeliveryTools._get_store_product`
   （`raise ValueError(f"{product_id} not found")`，服务于 `get_delivery_product_info`）。
3. 异常类型**恰为** `ValueError`（子类如 pydantic `ValidationError`、`UnicodeDecodeError` 不算）。
4. 最内层 traceback frame 的 code 就是该 helper 的 code，且 `environment.tools` 上的该 helper
   绑定**就是固定 Vita `DeliveryTools` 的原定义**（同名的临时/伪 toolkit 方法不认）。
5. 错误文本精确匹配 `^(?P<value>.+) not found$`，且 `value` 等于本次模型实际传入的某个字符串参数。

满足后：该工具**只执行一次**，结果以 `status="error"` 返回，文本沿用原生 `get_response` 形态
`Error: <原异常文本>`（不追加 `applied:false` 之类上游没有的业务断言）。错误经
`client_tool_result` → transcript(`error=True` 与原 call id) → 下一轮 Letta `tool_returns`
（同 id、同文本、`status="error"`）完整传递。

## 参数/结构错误：调用前拒绝

模型参数先经工具自身声明的 JSON schema 契约检查（`ae_adapter.validate_declared_arguments`）：
`required` 缺失、声明了 `properties` 时出现未声明键、`additionalProperties: false` 时出现额外键、
声明类型不符、`enum` 越界 → 直接以既有 `UpdateRejected` 形式回 error，**不执行工具**。
因此函数体内部的 `TypeError` 不会被当作“错参”放行；也不会为兼容不准确的测试双而放宽真实声明。
未实现完整 JSON Schema（仅上述子集），未覆盖的组合保持不限制并在此列明。

## 仍然阻断的情形（离线反例已覆盖）

- 非 READ（WRITE/THINK/GENERIC/未知）工具的任何异常；
- READ 工具内部 `RuntimeError`；
- READ 工具内部 `KeyError`/`IndexError`（结构性 lookup 一概不再全放）；
- 内部 `ValueError` 恰好含 `P1001` 等参数文本、或从嵌套/伪同名 helper 抛出；
- 返回值的序列化失败（转换在恢复判定之外，绝不降级为工具错误）；
- 重复 `tool_call_id`（不执行第二次）、超长回包、写后抛错（WRITE 永不走恢复路径，只执行一次）；
- 绑定抛 `RecoverableToolError` 但 `binding.read_only` 非真（adapter 二次校验）。

## 已知边界（本轮不改）

- **原生 `AssertionError` 已被 `@is_tool` 装饰器转成普通字符串返回**（`toolkit.py` 的 wrapper
  `except AssertionError: return str(e)`），因此断言型前置条件失败在桥接层表现为 **success 字符串**，
  不是异常。本轮原样保留，不重新猜成功/失败，也不改写上游。
- **冻结的评估轨迹只能承载 JSON object 参数**：`TraceList` 需要 `arguments` 为 dict。合法 JSON 但非
  object（数组/字符串/null）以及非法 JSON 会在下发前显式 `BridgeBlocked`（本轮把裸 `JSONDecodeError`
  收敛为显式 BridgeBlocked，但不扩轨迹 schema、不伪造 native 轨迹）。扩 schema 是未支持的缺口。
- 只登记了 `get_delivery_product_info` 的商品查找分支；其他 READ 工具的查找失败分支未核验，仍阻断。
- `LookupError`/`AssertionError`/`TypeError` 不再作为整类恢复；参数结构问题由调用前 schema 契约处理。

## 相关文件与验证

实现：`ae_adapter.py`（`RecoverableToolError`、`validate_declared_arguments`、冻结 dispatch、
read_only 复检）、`ae_task_run.py`（`native_read_only`、`verified_read_failure`、绑定包装、TraceList 显式停止）。
测试：`tests/test_ae_tool_errors.py`（真实 `vita.domains.delivery` 环境 + 真实 `@is_tool` 元数据；
脚本化 Letta transport，无模型/网络）。原始日志与 SHA 见
`results/ae-tool-errors-20260912-r1/`。本轮未部署、未跑付费模型；局部测试通过**不等于**完整云 t4 审计 PASS。
