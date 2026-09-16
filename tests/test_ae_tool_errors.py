"""Offline tool-error compatibility tests against the REAL fixed Vita classes.

The read-only classification, the empty-order return and the lookup failure are
exercised through the actual `vita.domains.delivery` environment and the real
`@is_tool` metadata, not only through test doubles. The scripted Letta transport
is a protocol fixture: no model, no network, no server.

Allowed recoverable scope (implemented in `ae_task_run.environment_bindings`):
only a binding classified READ from the native `ToolKitBase.tool_type` metadata
and only the explicitly registered, inspected native lookup branch (currently
`DeliveryTools._get_store_product` raising `ValueError(f"{product_id} not found")`
for `get_delivery_product_info`) is returned to the model, exactly once, as
`Error: <original text>` with `status="error"`. In addition, the one registered
WRITE precondition -- the pinned `DeliveryTools.pay_delivery_order` calling
`DeliveryTools._get_delivery_order`, which raises
`ValueError(f"Order {order_id} not found")` before any status write -- is returned
the same way, but only after the dedicated proof in
`ae_task_run.verified_write_precondition` establishes the fixed method/helper code
identity, the exact raise site, the order id of this very call, and an unchanged
native state hash. The tool keeps its real WRITE classification and every other
exception -- other ValueError/LookupError, the same text from another site or type,
a non-delivery order, internal KeyError/IndexError, TypeError, RuntimeError,
serialization failures, and any other WRITE/THINK/GENERIC/unknown tool -- still
stops the run. Argument structure problems are rejected before execution by the
declared-schema contract check, not by treating body TypeErrors as argument
errors. Other lookup branches are not yet supported and are documented as such.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for entry in (TESTS, ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from ae_adapter import (  # noqa: E402
    BridgeBlocked, LettaBridge, MemoryPolicy, RecoverableToolError,
    VerifiedWritePrecondition, dumps,
)
from ae_task_run import (TaskBridge, environment_bindings, native_read_only,  # noqa: E402
                         native_state_hash, native_write_precondition,
                         verified_write_precondition)
from test_ae_adapter import FakeTransport, approval, call, done  # noqa: E402

VITA_SOURCE_OVERRIDE = os.environ.get("AE_VITA_SOURCE")
VITA_SOURCE = Path(VITA_SOURCE_OVERRIDE) if VITA_SOURCE_OVERRIDE else Path(
    "/tmp/ae01-vita-8WHDvk/source")
DELIVERY_TOOLS = VITA_SOURCE / "src/vita/domains/delivery/tools.py"
MODEL_BASE = "http://127.0.0.1:8190/v1"
GOOD_PRODUCT = "P1"
BAD_PRODUCT = "P1001"
HALLUCINATED_ORDER = "O202406231430001"
STORE_DB = {
    "user_id": "U1",
    "orders": {},
    "stores": {"S1": {
        "store_id": "S1", "name": "Store One", "score": 4.5,
        "location": {"longitude": 1.0, "latitude": 2.0, "address": "Addr"},
        "tags": ["tea"],
        "products": [{"product_id": GOOD_PRODUCT, "name": "Milk Tea", "store_id": "S1",
                      "store_name": "Store One", "attributes": ["规格: 7分糖"],
                      "tags": ["tea"], "quantity": 1, "price": 10.0}],
    }},
}
BRIDGE_CONFIG = {"max_stage_posts": 64, "max_tool_return_chars": 26214}
# Same store, plus the fields a real `create_delivery_order` needs: the day's time
# (so a future dispatch_time is accepted) and an address registered in the native
# Location registry (so the real geo lookup resolves it).
CHAIN_DB = {
    **STORE_DB,
    "time": "2024-06-23 10:00:00",
    "location": [{"address": "Addr", "longitude": 1.0, "latitude": 2.0}],
}
CREATE_ARGS = {"user_id": "U1", "store_id": "S1", "product_ids": [GOOD_PRODUCT],
               "product_cnts": [1], "address": "Addr",
               "dispatch_time": "2024-06-23 15:00:00",
               "attributes": ["规格: 7分糖"]}


def _import_real_vita():
    """Import the real delivery environment with a dummy-key model config."""
    sys.path.insert(0, str(VITA_SOURCE / "src"))
    from vita.domains.delivery.environment import get_environment
    return get_environment


class _VitaFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if Path(sys.prefix).resolve() != (ROOT / ".venv-vita").resolve():
            raise unittest.SkipTest("real Vita tool tests require the project .venv-vita")
        # The project-wide `AE_VITA_SOURCE` convention: an explicitly declared
        # source must be real. A declared-but-missing fixed source is a hard error,
        # never a skip, so a server run cannot pass these acceptance checks without
        # the fixed Vita code. When nothing is declared the previous default+skip
        # behaviour is kept.
        if VITA_SOURCE_OVERRIDE and not DELIVERY_TOOLS.is_file():
            raise RuntimeError(
                "AE_VITA_SOURCE is set but the pinned Vita delivery tools are missing: "
                f"{DELIVERY_TOOLS}")
        if not (VITA_SOURCE / "src/vita").is_dir():
            raise unittest.SkipTest(f"fixed Vita source not present: {VITA_SOURCE}")
        prior_modules = {k: v for k, v in sys.modules.items()
                         if k == "vita" or k.startswith("vita.")}
        prior_path = list(sys.path)
        cls._tmp = TemporaryDirectory(prefix="ae-tool-errors-")
        config_path = Path(cls._tmp.name) / "models.yaml"
        config_path.write_text(json.dumps({"default": {}, "models": [
            {"name": "Qwen3-8B", "base_url": MODEL_BASE, "api_key": "EMPTY"}]}),
            encoding="utf-8")
        prior_env = os.environ.get("VITA_MODEL_CONFIG_PATH")
        os.environ["VITA_MODEL_CONFIG_PATH"] = str(config_path)

        def restore():
            os.environ.pop("VITA_MODEL_CONFIG_PATH", None)
            if prior_env is not None:
                os.environ["VITA_MODEL_CONFIG_PATH"] = prior_env
            for name in [n for n in sys.modules if n == "vita" or n.startswith("vita.")]:
                if name not in prior_modules:
                    sys.modules.pop(name, None)
            sys.modules.update(prior_modules)
            sys.path[:] = prior_path
            cls._tmp.cleanup()

        cls.addClassCleanup(restore)
        cls.get_environment = staticmethod(_import_real_vita())

    def environment(self, db=None):
        return self.get_environment(db=deepcopy(db or STORE_DB))

    def chain_environment(self):
        """The real delivery environment where create and pay both can run."""
        return self.get_environment(db=deepcopy(CHAIN_DB))

    @staticmethod
    def real_order(order_id, status="unpaid"):
        from vita.data_model.tasks import Order
        return Order.model_validate({
            "order_id": order_id, "order_type": "delivery", "user_id": "U1", "store_id": "S1",
            "location": {"longitude": 1.0, "latitude": 2.0, "address": "Addr"},
            "status": status,
            "products": [{"product_id": GOOD_PRODUCT, "name": "Milk Tea",
                          "attributes": ["规格: 7分糖"], "quantity": 1}],
        })


class NativeMetadataTests(_VitaFixture):
    def test_read_only_comes_from_native_tool_type_metadata(self):
        env = self.environment()
        self.assertTrue(native_read_only(env, "get_delivery_product_info"))
        self.assertTrue(native_read_only(env, "get_user_all_orders"))
        self.assertFalse(native_read_only(env, "create_delivery_order"))
        self.assertFalse(native_read_only(env, "delivery_distance_to_time"))  # GENERIC
        self.assertFalse(native_read_only(env, "no_such_tool"))
        bindings = environment_bindings(env)
        self.assertTrue(bindings["get_delivery_product_info"].read_only)
        self.assertFalse(bindings["create_delivery_order"].read_only)

    def test_empty_orders_return_stays_the_original_empty_string(self):
        bindings = environment_bindings(self.environment())
        self.assertEqual(bindings["get_user_all_orders"].call(), "")


class BridgeRecoverableReadTests(_VitaFixture):
    def make_bridge(self, responses, env=None):
        env = env or self.environment()
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, responses, aid="agent-tool-errors",
                                  bid="block-tool-errors")
        bridge = LettaBridge(transport, "agent-tool-errors", memory, max_rounds=8, max_steps=4)
        bridge.visible_refs.add("t4/history/0")
        return bridge, transport, env

    def count_calls(self, env):
        """Wrap the real environment's dispatcher, keeping the real tool call."""
        original = env.make_tool_call
        counter = {"calls": []}

        def counting(tool_name, requestor="assistant", **kwargs):
            counter["calls"].append(tool_name)
            return original(tool_name=tool_name, requestor=requestor, **kwargs)

        env.make_tool_call = counting
        return counter

    def test_read_failure_returns_error_then_valid_id_succeeds_once(self):
        env = self.environment()
        counter = self.count_calls(env)
        bridge, transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": BAD_PRODUCT})),
             approval(call("c2", "get_delivery_product_info", {"product_id": GOOD_PRODUCT})),
             done()], env=env)
        bindings = environment_bindings(env)
        messages = bridge.exchange([{"role": "user", "content": "fixture"}], bindings)
        self.assertTrue(messages)
        self.assertEqual(counter["calls"],
                         ["get_delivery_product_info", "get_delivery_product_info"])
        results = [e for e in bridge.trace if e["kind"] == "client_tool_result"]
        self.assertEqual([r["result"]["tool_call_id"] for r in results], ["c1", "c2"])
        self.assertEqual(results[0]["result"]["status"], "error")
        self.assertIn("P1001 not found", results[0]["result"]["tool_return"])
        self.assertEqual(results[1]["result"]["status"], "success")
        self.assertIn("StoreProduct", results[1]["result"]["tool_return"])
        posts = [body for method, _path, body in transport.requests
                 if method == "POST" and body and "tool_returns" in json.dumps(body)]
        returns = [r for body in posts for msg in body["messages"]
                   if msg.get("type") == "tool_return" for r in msg["tool_returns"]]
        self.assertEqual([r["tool_call_id"] for r in returns], ["c1", "c2"])
        self.assertEqual(returns[0]["status"], "error")
        self.assertIn("P1001 not found", returns[0]["tool_return"])
        self.assertEqual(returns[1]["status"], "success")
        # The failed call was not re-executed: each id ran exactly once.

    def test_undeclared_argument_is_rejected_without_dispatch(self):
        """A schema-external argument never reaches the native tool."""
        env = self.environment()
        counter = self.count_calls(env)
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info",
                           {"product_id": BAD_PRODUCT, "bogus": 1})), done()], env=env)
        bridge.exchange([{"role": "user", "content": "fixture"}], environment_bindings(env))
        self.assertEqual(counter["calls"], [])
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertEqual(result["result"]["status"], "error")
        self.assertIn("undeclared argument", result["result"]["tool_return"])

    def test_mistyped_argument_is_rejected_without_dispatch(self):
        env = self.environment()
        counter = self.count_calls(env)
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": 123})), done()],
            env=env)
        bridge.exchange([{"role": "user", "content": "fixture"}], environment_bindings(env))
        self.assertEqual(counter["calls"], [])
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertEqual(result["result"]["status"], "error")
        self.assertIn("declared type", result["result"]["tool_return"])

    def test_missing_required_argument_is_rejected_without_dispatch(self):
        env = self.environment()
        counter = self.count_calls(env)
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {})), done()], env=env)
        bridge.exchange([{"role": "user", "content": "fixture"}], environment_bindings(env))
        self.assertEqual(counter["calls"], [])
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertEqual(result["result"]["status"], "error")
        self.assertIn("missing required argument", result["result"]["tool_return"])

    def test_read_success_is_unchanged(self):
        env = self.environment()
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": GOOD_PRODUCT})),
             done()], env=env)
        bridge.exchange([{"role": "user", "content": "fixture"}],
                        environment_bindings(env))
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertEqual(result["result"]["status"], "success")

    def test_empty_orders_success_is_not_turned_into_an_error(self):
        env = self.environment()
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_user_all_orders", {})), done()], env=env)
        bridge.exchange([{"role": "user", "content": "fixture"}],
                        environment_bindings(env))
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertEqual(result["result"]["status"], "success")
        self.assertEqual(result["result"]["tool_return"], "")

    def test_duplicate_tool_call_id_is_not_executed_twice(self):
        env = self.environment()
        counter = self.count_calls(env)
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": BAD_PRODUCT})),
             approval(call("c1", "get_delivery_product_info", {"product_id": GOOD_PRODUCT}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertEqual(counter["calls"], ["get_delivery_product_info"])

    def test_internal_value_error_mentioning_the_argument_still_blocks(self):
        """A same-message ValueError from the tool body is not the verified site."""
        env = self.environment()
        env.tools._get_store_product = lambda product_id: (_ for _ in ()).throw(
            ValueError(f"{product_id} not found"))
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": BAD_PRODUCT}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))

    def test_forged_same_name_helper_in_a_subclass_still_blocks(self):
        """A same-named helper that is not the fixed Vita definition blocks."""
        env = self.environment()

        class ForgedTools(type(env.tools)):
            def _get_store_product(self, product_id):
                raise ValueError(f"{product_id} not found")

        forged = ForgedTools(env.tools.db)
        env.tools = forged
        # Even with a bound same-named method and the exact message, identity
        # against the pinned DeliveryTools definition fails.
        with self.assertRaises(ValueError):
            forged.use_tool("get_delivery_product_info", product_id=BAD_PRODUCT)
        with self.assertRaises(BridgeBlocked):
            memory = MemoryPolicy("erratum", {"p000": {"category": "c", "content": "x"}},
                                  block_char_limit=4000)
            transport = FakeTransport(memory, [approval(call(
                "c1", "get_delivery_product_info", {"product_id": BAD_PRODUCT}))], aid="a", bid="b")
            bridge = LettaBridge(transport, "a", memory, max_rounds=4, max_steps=2)
            bridge.visible_refs.add("t4/history/0")
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))

    def test_nested_raise_is_not_attributed_to_the_helper(self):
        """Only the innermost frame counts, so a wrapper re-raise blocks."""
        env = self.environment()
        real = env.tools._get_store_product
        helper_identity = {"ok": False}

        class ForgedTools(type(env.tools)):
            pass

        def nested(product_id):
            try:
                real(product_id)
            except ValueError as exc:
                raise ValueError(str(exc)) from None
        # Replace only the *instance* attribute: the binding then resolves a
        # non-original code path, so the branch must not be considered verified.
        env.tools._get_store_product = nested
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": BAD_PRODUCT}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        helper_identity["ok"] = True

    def test_closed_schema_rejects_extra_argument_with_empty_properties(self):
        """additionalProperties=False must reject extras even with no properties."""
        class Tool:
            openai_schema = {"type": "function", "function": {
                "name": "closed_tool", "description": "fixture",
                "parameters": {"type": "object", "properties": {},
                               "additionalProperties": False}}}

        calls = []

        class Env:
            def get_tools(self):
                return [Tool()]

            def make_tool_call(self, tool_name, requestor, **kwargs):
                calls.append(kwargs)
                return "ok"

            def to_json_str(self, value):
                return dumps(value)

        from ae_adapter import validate_declared_arguments
        self.assertIn("undeclared argument", validate_declared_arguments(
            Tool.openai_schema["function"], {"extra": 1}))
        self.assertIsNone(validate_declared_arguments(
            Tool.openai_schema["function"], {}))
        memory = MemoryPolicy("erratum", {"p000": {"category": "c", "content": "x"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, [approval(call("c1", "closed_tool", {"extra": 1})),
                                           done()], aid="a", bid="b")
        bridge = LettaBridge(transport, "a", memory, max_rounds=4, max_steps=2)
        bridge.visible_refs.add("t4/history/0")
        bridge.exchange([{"role": "user", "content": "fixture"}], environment_bindings(Env()))
        self.assertEqual(calls, [])
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertIn("undeclared argument", result["result"]["tool_return"])

    def test_internal_lookup_errors_still_block(self):
        """KeyError/IndexError are never recoverable, whatever the tool type."""
        for error in (KeyError("P1001"), IndexError("P1001")):
            with self.subTest(error=type(error).__name__):
                env = self.environment()

                def raising(_product_id, _error=error):
                    raise _error

                env.tools._get_store_product = raising
                bridge, _transport, env = self.make_bridge(
                    [approval(call("c1", "get_delivery_product_info",
                                   {"product_id": BAD_PRODUCT}))], env=env)
                with self.assertRaises(BridgeBlocked):
                    bridge.exchange([{"role": "user", "content": "fixture"}],
                                    environment_bindings(env))

    def test_read_internal_runtime_error_still_blocks(self):
        class ExplodingStores(dict):
            def values(self):
                raise RuntimeError("internal store index failure")

        env = self.environment()
        env.tools.db.stores = ExplodingStores()
        counter = self.count_calls(env)
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": GOOD_PRODUCT}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertEqual(counter["calls"], ["get_delivery_product_info"])

    def test_read_serialization_failure_still_blocks(self):
        env = self.environment()
        bindings = environment_bindings(env)

        def exploding_json(_value):
            raise TypeError("fixture serialization failure")

        env.to_json_str = exploding_json
        with self.assertRaises(TypeError) as caught:
            bindings["get_delivery_product_info"].call(product_id=GOOD_PRODUCT)
        self.assertNotIsInstance(caught.exception, RecoverableToolError)
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", {"product_id": GOOD_PRODUCT}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))


class DispatchAndTraceShapeTests(_VitaFixture):
    def make_bridge(self, responses, env):
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, responses, aid="agent-dispatch",
                                  bid="block-dispatch")
        bridge = LettaBridge(transport, "agent-dispatch", memory, max_rounds=8, max_steps=4)
        bridge.visible_refs.add("t4/history/0")
        return bridge, transport

    def counting_environment(self, env):
        original = env.make_tool_call
        seen = {"tools": []}

        def counting(tool_name, requestor="assistant", **kwargs):
            seen["tools"].append(tool_name)
            return original(tool_name=tool_name, requestor=requestor, **kwargs)

        env.make_tool_call = counting
        return seen

    def test_name_argument_cannot_redirect_dispatch(self):
        """Passing `_name` must not execute the named write tool."""
        env = self.environment()
        seen = self.counting_environment(env)
        bindings = environment_bindings(env)
        # Binding level: the frozen target forwards `_name` as an ordinary
        # argument, which the real tool rejects instead of switching tools.
        with self.assertRaises(TypeError):
            bindings["get_delivery_product_info"].call(
                product_id=GOOD_PRODUCT, _name="create_delivery_order")
        # The frozen target forwarded `_name` to the READ tool; no write dispatch.
        self.assertNotIn("create_delivery_order", seen["tools"])
        seen["tools"].clear()
        # Bridge level: the declared-schema contract rejects it before dispatch.
        bridge, _transport = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info",
                           {"product_id": GOOD_PRODUCT,
                            "_name": "create_delivery_order"})), done()], env)
        bridge.exchange([{"role": "user", "content": "fixture"}],
                        environment_bindings(env))
        self.assertEqual(seen["tools"], [])
        result = [e for e in bridge.trace if e["kind"] == "client_tool_result"][0]
        self.assertEqual(result["result"]["status"], "error")
        self.assertIn("undeclared argument", result["result"]["tool_return"])

    def test_non_object_json_arguments_stop_the_trace_layer(self):
        """Legal JSON that is not an object cannot enter the evaluator trace."""
        env = self.environment()
        seen = self.counting_environment(env)
        bridge, _transport = self.make_bridge(
            [approval(call("c1", "get_delivery_product_info", [1, 2]))], env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertEqual(seen["tools"], [])

    def test_malformed_json_arguments_stop_before_dispatch(self):
        """Malformed JSON stops explicitly instead of leaking a parse error."""
        env = self.environment()
        seen = self.counting_environment(env)
        malformed = {"tool_call_id": "c1", "name": "get_delivery_product_info",
                     "arguments": "{not json"}
        bridge, _transport = self.make_bridge([approval(malformed)], env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertEqual(seen["tools"], [])


class WriteAndUnknownToolTests(_VitaFixture):
    def make_bridge(self, responses, env):
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, responses, aid="agent-write",
                                  bid="block-write")
        bridge = LettaBridge(transport, "agent-write", memory, max_rounds=8, max_steps=4)
        bridge.visible_refs.add("t4/history/0")
        return bridge, transport

    def test_real_write_lookup_error_stops_without_recovery(self):
        env = self.environment()
        bridge, _transport = self.make_bridge(
            [approval(call("c1", "create_delivery_order", {
                "user_id": "U1", "store_id": "S1", "product_ids": [BAD_PRODUCT],
                "product_cnts": [1], "address": "Addr",
                "dispatch_time": "2030-06-23 15:00:00", "attributes": ["规格: 7分糖"]}))], env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))

    def test_write_mutation_then_throw_still_blocks_and_runs_once(self):
        env = self.environment()
        env.tools.db.user_id = "U1"
        # Build one real order object through the real model class.
        from vita.data_model.tasks import Order
        order = Order.model_validate({
            "order_id": "#D1", "order_type": "delivery", "user_id": "U1", "store_id": "S1",
            "location": {"longitude": 1.0, "latitude": 2.0, "address": "Addr"},
            "status": "unpaid",
            "products": [{"product_id": GOOD_PRODUCT, "name": "Milk Tea",
                          "attributes": ["规格: 7分糖"], "quantity": 1}],
        })
        attempts = {"setitem": 0}

        class ExplodingOrders(dict):
            def __setitem__(self, key, value):
                attempts["setitem"] += 1
                raise RuntimeError("fixture write failure after in-memory mutation")

        env.tools.db.orders = ExplodingOrders({"#D1": order})
        original = env.make_tool_call
        counter = {"calls": []}

        def counting(tool_name, requestor="assistant", **kwargs):
            counter["calls"].append(tool_name)
            return original(tool_name=tool_name, requestor=requestor, **kwargs)

        env.make_tool_call = counting
        self.assertEqual(order.status, "unpaid")
        bridge, _transport = self.make_bridge(
            [approval(call("c1", "pay_delivery_order", {"order_id": "#D1"}))], env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertEqual(counter["calls"], ["pay_delivery_order"])
        self.assertEqual(attempts["setitem"], 1)
        # The real tool mutated the in-memory object before the throw.
        self.assertEqual(order.status, "paid")

    def test_write_binding_raising_recoverable_error_still_blocks(self):
        """The adapter checks read_only too, not just the exception class."""
        class Tool:
            openai_schema = {"type": "function", "function": {
                "name": "write_thing", "description": "fixture",
                "parameters": {"type": "object",
                               "properties": {"value": {"type": "string"}}}}}

        class Env:
            def get_tools(self):
                return [Tool()]

            def make_tool_call(self, tool_name, requestor, **kwargs):
                raise RecoverableToolError("lookup failed")

            def to_json_str(self, value):
                return dumps(value)

            @property
            def tools(self):
                class Toolkit:
                    def tool_type(self, name):
                        return "write"

                return Toolkit()

        bindings = environment_bindings(Env())
        self.assertFalse(bindings["write_thing"].read_only)
        with self.assertRaises(BridgeBlocked):
            memory = MemoryPolicy("erratum", {"p000": {"category": "c", "content": "x"}},
                                  block_char_limit=4000)
            transport = FakeTransport(memory, [approval(call("c1", "write_thing",
                                                             {"value": "x"}))], aid="a", bid="b")
            bridge = LettaBridge(transport, "a", memory, max_rounds=4, max_steps=2)
            bridge.visible_refs.add("t4/history/0")
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(Env()))

    def test_name_looks_read_only_but_metadata_says_otherwise(self):
        class Tool:
            openai_schema = {"type": "function", "function": {
                "name": "get_fake_lookup", "description": "fixture",
                "parameters": {"type": "object", "properties": {}}}}

        class Env:
            def __init__(self, tool_type=None):
                self.tool_type_value = tool_type

            def get_tools(self):
                return [Tool()]

            def make_tool_call(self, tool_name, requestor, **kwargs):
                raise ValueError("lookup failed")

            def to_json_str(self, value):
                return dumps(value)

            @property
            def tools(self):
                if self.tool_type_value is None:
                    return object()  # no native metadata accessor
                env = self

                class Toolkit:
                    def tool_type(self, name):
                        return env.tool_type_value

                return Toolkit()

        for tool_type in (None, "write", "generic", "think"):
            with self.subTest(tool_type=tool_type):
                bindings = environment_bindings(Env(tool_type))
                self.assertFalse(bindings["get_fake_lookup"].read_only)
                with self.assertRaises(ValueError) as caught:
                    bindings["get_fake_lookup"].call()
                self.assertNotIsInstance(caught.exception, RecoverableToolError)

    def test_absent_metadata_accessor_never_allows_recovery(self):
        class Tool:
            openai_schema = {"type": "function", "function": {
                "name": "get_orders", "description": "fixture",
                "parameters": {"type": "object", "properties": {}}}}

        class Env:
            def get_tools(self):
                return [Tool()]

            def make_tool_call(self, tool_name, requestor, **kwargs):
                raise ValueError("lookup failed")

            def to_json_str(self, value):
                return dumps(value)

        bindings = environment_bindings(Env())
        self.assertFalse(bindings["get_orders"].read_only)
        with self.assertRaises(BridgeBlocked):
            memory = MemoryPolicy("erratum", {"p000": {"category": "c", "content": "x"}},
                                  block_char_limit=4000)
            transport = FakeTransport(memory, [approval(call("c1", "get_orders", {}))],
                                      aid="a", bid="b")
            bridge = LettaBridge(transport, "a", memory, max_rounds=4, max_steps=2)
            bridge.visible_refs.add("t4/history/0")
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(Env()))


class TranscriptFlowTests(_VitaFixture):
    def test_error_flag_reaches_transcript_and_next_letta_request(self):
        env = self.environment()
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(
            memory,
            [approval(call("c1", "get_delivery_product_info", {"product_id": BAD_PRODUCT})),
             done("fixture finished")],
            aid="agent-transcript", bid="block-transcript")
        bridge = TaskBridge(transport, "agent-transcript", memory, max_rounds=8, max_steps=4,
                            config=BRIDGE_CONFIG, emit=lambda _event: None,
                            include_memory_tool=False)
        bridge.begin({"subtask_id": "sub_U000828_4"},
                     {"role": "assistant", "content": "fixture greeting"})
        bridge.visible_refs.add("t4/user/0")
        bridge.exchange([{"role": "user", "content": dumps(
            {"source": "current_task", "ref": "t4/user/0",
             "instruction": "fixture instruction"})}], environment_bindings(env))
        tool_rows = [row for row in bridge.transcript if row.get("role") == "tool"]
        self.assertEqual(len(tool_rows), 1)
        self.assertEqual(tool_rows[0]["id"], "c1")
        self.assertIs(tool_rows[0]["error"], True)
        self.assertIn("P1001 not found", tool_rows[0]["content"])
        events = [e for e in bridge.trace if e["kind"] == "client_tool_result"]
        self.assertEqual(events[0]["result"]["status"], "error")
        posts = [body for method, _path, body in transport.requests
                 if method == "POST" and body and "tool_returns" in json.dumps(body)]
        returned = [r for body in posts for msg in body["messages"]
                    if msg.get("type") == "tool_return" for r in msg["tool_returns"]]
        self.assertEqual(returned[0]["tool_call_id"], "c1")
        self.assertEqual(returned[0]["status"], "error")
        self.assertIn("P1001 not found", returned[0]["tool_return"])


class WritePreconditionProofTests(_VitaFixture):
    """The registered WRITE precondition proof, dimension by dimension.

    Everything runs against the REAL fixed Vita `DeliveryTools`. Each test breaks
    exactly one requirement of `ae_task_run.verified_write_precondition` and shows
    the proof is refused, so no single check can carry the others.
    """

    def capture_missing(self, env, order_id=HALLUCINATED_ORDER):
        before = native_state_hash(env)
        try:
            env.tools.use_tool("pay_delivery_order", order_id=order_id)
        except ValueError as exc:
            return before, exc
        raise AssertionError("the missing-order branch must raise ValueError")

    def test_the_registered_branch_is_proven_only_for_this_call(self):
        env = self.chain_environment()
        before, exc = self.capture_missing(env)
        self.assertTrue(native_write_precondition(env, "pay_delivery_order"))
        self.assertTrue(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, before))
        # A different supplied id cannot borrow the same message.
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": "O-somewhere-else"}, before))
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {}, before))

    def test_a_non_delivery_order_error_is_not_the_registered_branch(self):
        env = self.chain_environment()
        env.tools.db.orders["#INSTORE#1"] = self.real_order("#INSTORE#1")
        env.tools.db.orders["#INSTORE#1"].order_type = "instore"
        before = native_state_hash(env)
        with self.assertRaises(ValueError) as caught:
            env.tools.use_tool("pay_delivery_order", order_id="#INSTORE#1")
        self.assertIn("is not a delivery order", str(caught.exception))
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", caught.exception, {"order_id": "#INSTORE#1"}, before))

    def test_the_same_text_on_another_exception_type_is_not_proven(self):
        env = self.chain_environment()
        before, exc = self.capture_missing(env)
        try:
            raise RuntimeError(str(exc)) from None
        except RuntimeError as other:
            self.assertFalse(verified_write_precondition(
                env, "pay_delivery_order", other, {"order_id": HALLUCINATED_ORDER}, before))

    def test_a_reraise_from_another_frame_is_not_proven(self):
        env = self.chain_environment()
        before = native_state_hash(env)

        def nested(order_id):
            try:
                env.tools._get_delivery_order(order_id)
            except ValueError as exc:
                raise ValueError(str(exc)) from None

        with self.assertRaises(ValueError) as caught:
            nested(HALLUCINATED_ORDER)
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", caught.exception,
            {"order_id": HALLUCINATED_ORDER}, before))

    def test_a_forged_helper_is_not_proven(self):
        env = self.chain_environment()

        class Forged(type(env.tools)):
            def _get_delivery_order(self, order_id=None):
                raise ValueError(f"Order {order_id} not found")

        forged = Forged(env.tools.db)
        before = native_state_hash(forged)
        with self.assertRaises(ValueError) as caught:
            forged.use_tool("pay_delivery_order", order_id=HALLUCINATED_ORDER)
        self.assertFalse(native_write_precondition(forged, "pay_delivery_order"))
        self.assertFalse(verified_write_precondition(
            forged, "pay_delivery_order", caught.exception,
            {"order_id": HALLUCINATED_ORDER}, before))

    def test_a_proof_needs_an_unchanged_state_witness(self):
        env = self.chain_environment()
        before, exc = self.capture_missing(env)
        # A witness taken before some other write is stale: the state moved, so the
        # failure cannot be attributed to a pure precondition path any more.
        env.tools.db.orders["#OTHER"] = self.real_order("#OTHER")
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, before))
        # An environment that cannot produce its own state hash is never proven.
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, None))

    def test_a_wrapper_pay_method_is_not_authorized(self):
        """A wrapper that writes first and then calls the real helper is refused."""
        env = self.chain_environment()

        class Wrapping(type(env.tools)):
            def pay_delivery_order(self, order_id):
                self.db.orders["#WROTE-FIRST"] = self._real_factory("#WROTE-FIRST")
                return type(env.tools).pay_delivery_order(self, order_id)

            @staticmethod
            def _real_factory(order_id):
                from vita.data_model.tasks import Order
                return Order.model_validate({
                    "order_id": order_id, "order_type": "delivery", "user_id": "U1",
                    "store_id": "S1", "status": "unpaid", "products": []})

        wrapped = Wrapping(env.tools.db)
        self.assertFalse(native_write_precondition(wrapped, "pay_delivery_order"))
        with self.assertRaises(ValueError):
            wrapped.use_tool("pay_delivery_order", order_id=HALLUCINATED_ORDER)
        self.assertIn("#WROTE-FIRST", wrapped.db.orders)


class ScriptedRecoveryTransport(FakeTransport):
    """A scripted model stand-in whose replies follow the REAL tool returns.

    The order id used for the second payment is read out of the actual
    `create_delivery_order` return; nothing here is hand-filled.
    """

    def __init__(self, memory, missing_id, *, aid, bid):
        super().__init__(memory, (), aid=aid, bid=bid)
        self.missing_id = missing_id
        self.stage = "pay-missing"
        self.extracted_ids = []
        self.error_return = None
        self.create_return = None
        self.final_return = None

    def request(self, method, path, body=None):
        if method == "POST":
            self.responses = [self._next_reply(body)]
        return super().request(method, path, body)

    @staticmethod
    def _returns(body):
        returned = []
        for message in (body or {}).get("messages", []):
            if message.get("type") == "tool_return":
                returned.extend(message["tool_returns"])
        return returned

    def _next_reply(self, body):
        returns = self._returns(body)
        if self.stage == "pay-missing":
            self.stage = "read-error"
            return approval(call("c1", "pay_delivery_order", {"order_id": self.missing_id}))
        if self.stage == "read-error":
            self.error_return = deepcopy(returns[-1])
            assert self.error_return["status"] == "error", self.error_return
            self.stage = "read-create"
            return approval(call("c2", "create_delivery_order", CREATE_ARGS))
        if self.stage == "read-create":
            self.create_return = deepcopy(
                [item for item in returns if item["tool_call_id"] == "c2"][0])
            assert self.create_return["status"] == "success", self.create_return
            match = re.search(r"order_id[:=]['\"]?([^,'\")\s]+)", self.create_return["tool_return"])
            assert match, self.create_return["tool_return"]
            self.extracted_ids.append(match.group(1))
            self.stage = "read-pay"
            return approval(call("c3", "pay_delivery_order", {"order_id": match.group(1)}))
        if self.stage == "read-pay":
            self.final_return = deepcopy(
                [item for item in returns if item["tool_call_id"] == "c3"][0])
            self.stage = "done"
        return done("fixture finished")


class WritePreconditionRecoveryTests(_VitaFixture):
    """The real bridge returns the proven WRITE precondition error to the model."""

    def make_bridge(self, responses=(), env=None, *, max_steps=4):
        env = env or self.environment()
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, responses, aid="agent-write-precondition",
                                  bid="block-write-precondition")
        bridge = LettaBridge(transport, "agent-write-precondition", memory,
                             max_rounds=8, max_steps=max_steps)
        bridge.visible_refs.add("t4/history/0")
        return bridge, transport, env

    def test_the_missing_order_error_is_returned_once_with_the_same_id(self):
        env = self.environment()
        before = env.tools.db.model_dump(mode="json")
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "pay_delivery_order", {"order_id": HALLUCINATED_ORDER})),
             done()], env=env)
        bindings = environment_bindings(env)
        self.assertTrue(bindings["pay_delivery_order"].write_precondition)
        self.assertFalse(bindings["pay_delivery_order"].read_only)
        bridge.exchange([{"role": "user", "content": "fixture"}], bindings)
        results = [e for e in bridge.trace if e["kind"] == "client_tool_result"]
        self.assertEqual(len(results), 1, "exactly one result for one call, no retry")
        self.assertEqual(results[0]["result"]["tool_call_id"], "c1")
        self.assertEqual(results[0]["result"]["status"], "error")
        self.assertEqual(results[0]["result"]["tool_return"],
                         f"Error: Order {HALLUCINATED_ORDER} not found")
        # The full native database is byte-identical: nothing was written.
        self.assertEqual(before, env.tools.db.model_dump(mode="json"))

    def test_the_error_reaches_the_transcript_and_the_next_request(self):
        env = self.environment()
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(
            memory,
            [approval(call("c1", "pay_delivery_order", {"order_id": HALLUCINATED_ORDER})),
             done("fixture finished")], aid="agent-transcript-pre", bid="block-transcript-pre")
        bridge = TaskBridge(transport, "agent-transcript-pre", memory, max_rounds=8, max_steps=4,
                            config=BRIDGE_CONFIG, emit=lambda _event: None,
                            include_memory_tool=False)
        bridge.begin({"subtask_id": "sub_U000828_4"},
                     {"role": "assistant", "content": "fixture greeting"})
        bridge.visible_refs.add("t4/user/0")
        bridge.exchange([{"role": "user", "content": dumps(
            {"source": "current_task", "ref": "t4/user/0",
             "instruction": "fixture instruction"})}], environment_bindings(env))
        rows = [row for row in bridge.transcript if row.get("role") == "tool"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "c1")
        self.assertIs(rows[0]["error"], True)
        self.assertIn(f"Order {HALLUCINATED_ORDER} not found", rows[0]["content"])
        posts = [body for method, _path, body in transport.requests
                 if method == "POST" and body and "tool_returns" in json.dumps(body)]
        returned = [r for body in posts for message in body["messages"]
                    if message.get("type") == "tool_return"
                    for r in message["tool_returns"]]
        self.assertEqual([r["tool_call_id"] for r in returned], ["c1"])
        self.assertEqual(returned[0]["status"], "error")
        self.assertIn(f"Order {HALLUCINATED_ORDER} not found", returned[0]["tool_return"])

    def test_a_real_create_then_pay_chain_pays_the_real_order(self):
        """A fixture recovery chain: the real return supplies the id, not the test."""
        env = self.chain_environment()
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = ScriptedRecoveryTransport(memory, HALLUCINATED_ORDER,
                                              aid="agent-chain", bid="block-chain")
        bridge = LettaBridge(transport, "agent-chain", memory, max_rounds=8, max_steps=6)
        bridge.visible_refs.add("t4/history/0")
        bridge.exchange([{"role": "user", "content": "fixture"}], environment_bindings(env))
        results = [e["result"] for e in bridge.trace if e["kind"] == "client_tool_result"]
        self.assertEqual([r["status"] for r in results], ["error", "success", "success"])
        self.assertEqual(results[0]["tool_return"],
                         f"Error: Order {HALLUCINATED_ORDER} not found")
        self.assertEqual(len(transport.extracted_ids), 1)
        order_id = transport.extracted_ids[0]
        self.assertNotEqual(order_id, HALLUCINATED_ORDER)
        self.assertIn(f"order_id:{order_id}", transport.create_return["tool_return"])
        self.assertEqual(env.tools.db.orders[order_id].status, "paid")
        self.assertEqual(len(env.tools.db.orders), 1)
        self.assertEqual(transport.final_return["status"], "success")
        self.assertIn("Payment successful", transport.final_return["tool_return"])

    def test_a_write_that_moves_the_state_is_not_returned_as_an_error(self):
        """The same real raise site, but a write happened first: still blocked."""
        env = self.chain_environment()
        injected = self.real_order("#INJECTED")

        class WritingOrders(dict):
            def __contains__(self, key):
                if not dict.__contains__(self, key):
                    self["#INJECTED"] = injected
                return dict.__contains__(self, key)

        env.tools.db.orders = WritingOrders(env.tools.db.orders)
        before = env.tools.db.model_dump(mode="json")
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "pay_delivery_order", {"order_id": HALLUCINATED_ORDER}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertNotEqual(before, env.tools.db.model_dump(mode="json"))
        self.assertIn("#INJECTED", env.tools.db.orders)

    def test_a_wrapping_pay_method_still_blocks(self):
        env = self.chain_environment()
        wrote = {"orders": 0}
        real_tools = type(env.tools)

        class Wrapping(type(env.tools)):
            def pay_delivery_order(self, order_id):
                wrote["orders"] += 1
                self.db.orders["#WROTE-FIRST"] = real
                return real_tools.pay_delivery_order(self, order_id)

        from vita.data_model.tasks import Order
        real = Order.model_validate({
            "order_id": "#WROTE-FIRST", "order_type": "delivery", "user_id": "U1",
            "store_id": "S1", "status": "unpaid", "products": []})
        wrapped = Wrapping(env.tools.db)
        env.tools = wrapped
        bridge, _transport, env = self.make_bridge(
            [approval(call("c1", "pay_delivery_order", {"order_id": HALLUCINATED_ORDER}))],
            env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        self.assertEqual(wrote["orders"], 1)
        self.assertIn("#WROTE-FIRST", env.tools.db.orders)

    def test_a_serialization_failure_on_a_successful_pay_still_blocks(self):
        env = self.chain_environment()
        env.tools.db.orders["#D1"] = self.real_order("#D1")

        def exploding_json(_value):
            raise TypeError("fixture serialization failure")

        env.to_json_str = exploding_json
        bindings = environment_bindings(env)
        with self.assertRaises(TypeError) as caught:
            bindings["pay_delivery_order"].call(order_id="#D1")
        self.assertNotIsInstance(caught.exception, VerifiedWritePrecondition)
        self.assertNotIsInstance(caught.exception, RecoverableToolError)
        self.assertEqual(env.tools.db.orders["#D1"].status, "paid")

    def test_the_write_flag_is_never_set_for_other_native_tools(self):
        env = self.chain_environment()
        bindings = environment_bindings(env)
        self.assertTrue(bindings["pay_delivery_order"].write_precondition)
        for name, binding in bindings.items():
            if name != "pay_delivery_order":
                self.assertFalse(binding.write_precondition, name)
        # A double without the fixed classes can never be authorized.
        self.assertFalse(native_write_precondition(object(), "pay_delivery_order"))


class MulticallBatchPreconditionTests(_VitaFixture):
    """A declared batch keeps every call; an unknown WRITE error still stops.

    These run the real multicall compatibility policy, the real delivery
    environment and the real bridge: no call is dropped, reordered or replayed,
    and a proven precondition failure inside a batch does not hide the other
    calls' returns.
    """

    def make_bridge(self, responses, env):
        from ae_multicall import CURRENT_POLICY
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, responses, aid="agent-batch-pre",
                                  bid="block-batch-pre")
        bridge = LettaBridge(transport, "agent-batch-pre", memory, max_rounds=8, max_steps=4,
                             multicall=CURRENT_POLICY)
        bridge.visible_refs.add("t4/history/0")
        return bridge, transport, env

    def test_a_precondition_failure_does_not_drop_the_other_call(self):
        env = self.chain_environment()
        first, second = ("01a094c62aacd8ab46b11b69c6e81881",
                         "01a094c62aacd8ab46b11b69c6e81882")
        bridge, transport, env = self.make_bridge(
            [approval(call(first, "pay_delivery_order", {"order_id": HALLUCINATED_ORDER}),
                      call(second, "create_delivery_order", CREATE_ARGS)),
             done()], env=env)
        bridge.exchange([{"role": "user", "content": "fixture"}], environment_bindings(env))
        rows = [e for e in bridge.trace if e["kind"] == "client_tool_result"]
        self.assertEqual([r["call"]["tool_call_id"] for r in rows], [first, second])
        self.assertEqual([r["result"]["status"] for r in rows], ["error", "success"])
        self.assertEqual(rows[0]["result"]["tool_return"],
                         f"Error: Order {HALLUCINATED_ORDER} not found")
        self.assertEqual(len(env.tools.db.orders), 1)
        # Both returns travel in ONE POST, in provider order, with native ids.
        posts = [body for method, _path, body in transport.requests
                 if method == "POST" and body and "tool_returns" in json.dumps(body)]
        returned = [r for body in posts for message in body["messages"]
                    if message.get("type") == "tool_return"
                    for r in message["tool_returns"]]
        self.assertEqual([r["tool_call_id"] for r in returned], [first, second])
        self.assertEqual([r["status"] for r in returned], ["error", "success"])

    def test_an_unknown_write_error_stops_and_keeps_the_executed_evidence(self):
        env = self.chain_environment()
        first, second = ("01a094c62aacd8ab46b11b69c6e81883",
                         "01a094c62aacd8ab46b11b69c6e81884")
        bridge, _transport, env = self.make_bridge(
            [approval(call(first, "create_delivery_order", CREATE_ARGS),
                      call(second, "modify_delivery_order",
                           {"order_id": "O-missing", "note": "fixture"}))], env=env)
        with self.assertRaises(BridgeBlocked):
            bridge.exchange([{"role": "user", "content": "fixture"}],
                            environment_bindings(env))
        rows = [e for e in bridge.trace if e["kind"] == "client_tool_result"]
        # The executed call's evidence is retained; the failing call is not replayed
        # and no return is submitted for the part-executed batch.
        self.assertEqual([r["call"]["tool_call_id"] for r in rows], [first])
        self.assertEqual(rows[0]["result"]["status"], "success")
        self.assertEqual(len(env.tools.db.orders), 1)
        self.assertIn("bridge_blocked", [e["kind"] for e in bridge.trace])


class WritePreconditionWitnessTests(_VitaFixture):
    """The state witness must come from the fixed chain and be a valid digest.

    The r1 review showed the proof compared `str()` of whatever `get_db_hash`
    returned, so a replaced getter returning an empty string or a cached old digest
    let a real write through. Every replacement is refused here even when its value
    looks like a real digest; only the untouched native chain is a witness. Equal
    digests are an additional state comparison, not proof of no writes.
    """

    def writing_environment(self):
        """A real environment whose missing-order lookup writes before it raises."""
        env = self.chain_environment()
        injected = self.real_order("#INJECTED")

        class WritingOrders(dict):
            def __contains__(self, key):
                if not dict.__contains__(self, key):
                    self["#INJECTED"] = injected
                return dict.__contains__(self, key)

        env.tools.db.orders = WritingOrders(env.tools.db.orders)
        return env

    def capture(self, env, order_id=HALLUCINATED_ORDER):
        try:
            env.tools.use_tool("pay_delivery_order", order_id=order_id)
        except ValueError as exc:
            return exc
        raise AssertionError("the missing-order branch must raise ValueError")

    def test_the_untouched_native_chain_is_a_valid_witness(self):
        env = self.chain_environment()
        digest = native_state_hash(env)
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)
        self.assertTrue(native_write_precondition(env, "pay_delivery_order"))

    def test_the_digest_contract_is_strict(self):
        from ae_task_run import _valid_state_digest
        self.assertTrue(_valid_state_digest("0" * 64))
        self.assertTrue(_valid_state_digest("abcdef0123456789" * 4))
        for bad in ("", None, 0, b"0" * 64, "0" * 63, "0" * 65, "A" * 64, "g" * 64):
            with self.subTest(value=repr(bad)):
                self.assertFalse(_valid_state_digest(bad))

    def test_a_write_before_the_raise_is_detected(self):
        env = self.writing_environment()
        before = native_state_hash(env)
        exc = self.capture(env)
        self.assertIsNotNone(before)
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, before))
        self.assertIn("#INJECTED", env.tools.db.orders)

    def test_an_empty_or_invalid_witness_cannot_hide_a_write(self):
        factory_reasons = {
            "empty string": lambda: "",
            "none": lambda: None,
            "bytes": lambda: b"0" * 64,
            "short": lambda: "0" * 63,
            "uppercase": lambda: "A" * 64,
            "non-hex": lambda: "g" * 64,
        }
        for label, make in factory_reasons.items():
            with self.subTest(witness=label):
                env = self.writing_environment()
                stale = native_state_hash(env)
                self.assertIsNotNone(stale)
                # The getter is replaced AFTER a real digest was captured; the
                # r1 comparison would have accepted the fabricated value.
                env.get_db_hash = make
                self.assertIsNone(native_state_hash(env))
                exc = self.capture(env)
                self.assertFalse(verified_write_precondition(
                    env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, stale))
                self.assertFalse(native_write_precondition(env, "pay_delivery_order"))

    def test_a_cached_stale_digest_cannot_hide_a_write(self):
        env = self.writing_environment()
        stale = native_state_hash(env)
        self.assertIsNotNone(stale)
        env.get_db_hash = lambda: stale
        self.assertIsNone(native_state_hash(env))
        exc = self.capture(env)
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, stale))

    def test_a_replaced_toolkit_getter_is_refused(self):
        env = self.writing_environment()
        stale = native_state_hash(env)
        self.assertIsNotNone(stale)
        env.tools.get_db_hash = lambda: stale
        self.assertIsNone(native_state_hash(env))
        exc = self.capture(env)
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", exc, {"order_id": HALLUCINATED_ORDER}, stale))
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))

    def test_a_subclass_override_is_refused(self):
        real = type(self.chain_environment())

        class Overriding(real):
            def get_db_hash(self):
                return "0" * 64

        env = self.chain_environment()
        overriding = Overriding(env.domain_name, env.policy, env.tools)
        self.assertIsNone(native_state_hash(overriding))

        class OverridingTools(type(env.tools)):
            def get_db_hash(self):
                return "0" * 64

        env.tools = OverridingTools(env.tools.db)
        self.assertIsNone(native_state_hash(env))
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))

    def test_a_raising_fixed_chain_is_refused(self):
        env = self.chain_environment()

        class BrokenDb:
            def model_dump(self, *_args, **_kwargs):
                raise RuntimeError("fixture database dump failure")

        env.tools.db = BrokenDb()
        self.assertIsNone(native_state_hash(env))
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))

    def test_a_replaced_getter_never_authorizes_the_binding(self):
        """Even with no write at all, a replaced getter must stop the run."""
        for label, make in {"empty": lambda: "", "stale": lambda: "0" * 64}.items():
            with self.subTest(getter=label):
                env = self.chain_environment()
                env.get_db_hash = make
                bindings = environment_bindings(env)
                self.assertFalse(bindings["pay_delivery_order"].write_precondition)
                memory = MemoryPolicy("erratum", {"p000": {"category": "c", "content": "x"}},
                                      block_char_limit=4000)
                transport = FakeTransport(
                    memory, [approval(call("c1", "pay_delivery_order",
                                           {"order_id": HALLUCINATED_ORDER}))],
                    aid="agent-witness", bid="block-witness")
                bridge = LettaBridge(transport, "agent-witness", memory,
                                     max_rounds=4, max_steps=2)
                bridge.visible_refs.add("t4/history/0")
                with self.assertRaises(BridgeBlocked):
                    bridge.exchange([{"role": "user", "content": "fixture"}],
                                    environment_bindings(env))
                self.assertEqual(
                    [e for e in bridge.trace if e["kind"] == "client_tool_result"], [])


class WritePreconditionObjectBindingTests(_VitaFixture):
    """A fixed definition bound to ANOTHER object is not this object's witness.

    The r2 review borrowed `other.tools.get_db_hash` (same `__func__`, different
    `__self__`) into this environment, so the fixed `Environment.get_db_hash` kept
    calling `self.tools.get_db_hash` and returned the OTHER database's digest. Every
    identity check therefore compares the bound object too, for the witness chain
    and for the pay method/helper, not only the function definition.
    """

    def other_writing_environment(self):
        """A second real environment whose lookup writes before it raises."""
        env = self.chain_environment()
        injected = self.real_order("#INJECTED")

        class WritingOrders(dict):
            def __contains__(self, key):
                if not dict.__contains__(self, key):
                    self["#INJECTED"] = injected
                return dict.__contains__(self, key)

        env.tools.db.orders = WritingOrders(env.tools.db.orders)
        return env

    def make_bridge(self, env):
        memory = MemoryPolicy("erratum", {"p000": {"category": "饮食偏好",
                                                   "content": "奶茶偏好5分糖"}},
                              block_char_limit=4000)
        transport = FakeTransport(memory, [], aid="agent-binding", bid="block-binding")
        bridge = LettaBridge(transport, "agent-binding", memory, max_rounds=8, max_steps=4)
        bridge.visible_refs.add("t4/history/0")
        return bridge

    def dispatch(self, bridge, env):
        return bridge._execute(
            {"tool_call_id": "binding-payment", "name": "pay_delivery_order",
             "arguments": dumps({"order_id": HALLUCINATED_ORDER})},
            environment_bindings(env))

    def test_the_control_still_recovers_and_authorizes(self):
        env = self.chain_environment()
        other = self.chain_environment()
        self.assertIsNotNone(native_state_hash(env))
        self.assertTrue(native_write_precondition(env, "pay_delivery_order"))
        self.assertIsNotNone(native_state_hash(other))
        bridge = self.make_bridge(env)
        result = self.dispatch(bridge, env)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["tool_call_id"], "binding-payment")
        self.assertEqual(env.tools.db.orders, {})
        self.assertEqual(other.tools.db.orders, {})

    def test_a_borrowed_toolkit_getter_does_not_authorize_even_without_a_write(self):
        env = self.chain_environment()
        other = self.chain_environment()
        env.tools.get_db_hash = other.tools.get_db_hash
        # Same function definition, different bound toolkit.
        self.assertIs(env.tools.get_db_hash.__func__, other.tools.get_db_hash.__func__)
        self.assertIsNot(env.tools.get_db_hash.__self__, env.tools)
        self.assertIsNone(native_state_hash(env))
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))
        bridge = self.make_bridge(env)
        with self.assertRaises(BridgeBlocked):
            self.dispatch(bridge, env)
        self.assertEqual([e for e in bridge.trace if e["kind"] == "client_tool_result"], [])

    def test_a_borrowed_environment_getter_is_refused(self):
        env = self.chain_environment()
        other = self.chain_environment()
        env.get_db_hash = other.get_db_hash
        self.assertIs(env.get_db_hash.__func__, other.get_db_hash.__func__)
        self.assertIsNot(env.get_db_hash.__self__, env)
        self.assertIsNone(native_state_hash(env))
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))

    def test_a_borrowed_witness_cannot_hide_a_write_here(self):
        """The borrowed digest is the other db's: this db's write must still stop."""
        env = self.other_writing_environment()
        other = self.chain_environment()
        stale_here = native_state_hash(env)
        self.assertIsNotNone(stale_here)
        env.tools.get_db_hash = other.tools.get_db_hash
        self.assertIsNone(native_state_hash(env))
        try:
            env.tools.use_tool("pay_delivery_order", order_id=HALLUCINATED_ORDER)
        except ValueError as exc:
            captured = exc
        else:  # pragma: no cover - the missing-order branch must raise
            self.fail("the missing-order branch must raise ValueError")
        self.assertFalse(verified_write_precondition(
            env, "pay_delivery_order", captured, {"order_id": HALLUCINATED_ORDER}, stale_here))
        bridge = self.make_bridge(env)
        with self.assertRaises(BridgeBlocked):
            self.dispatch(bridge, env)
        self.assertIn("#INJECTED", env.tools.db.orders)
        self.assertEqual(other.tools.db.orders, {})

    def test_a_borrowed_pay_method_is_refused(self):
        env = self.chain_environment()
        other = self.chain_environment()
        env.tools.pay_delivery_order = other.tools.pay_delivery_order
        self.assertIs(env.tools.pay_delivery_order.__func__,
                      other.tools.pay_delivery_order.__func__)
        self.assertIsNot(env.tools.pay_delivery_order.__self__, env.tools)
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))
        bridge = self.make_bridge(env)
        with self.assertRaises(BridgeBlocked):
            self.dispatch(bridge, env)

    def test_a_borrowed_helper_is_refused(self):
        env = self.chain_environment()
        other = self.chain_environment()
        env.tools._get_delivery_order = other.tools._get_delivery_order
        self.assertIs(env.tools._get_delivery_order.__func__,
                      other.tools._get_delivery_order.__func__)
        self.assertIsNot(env.tools._get_delivery_order.__self__, env.tools)
        self.assertFalse(native_write_precondition(env, "pay_delivery_order"))
        bridge = self.make_bridge(env)
        with self.assertRaises(BridgeBlocked):
            self.dispatch(bridge, env)

    def test_a_swap_after_binding_construction_is_caught_per_call(self):
        """The authorization is re-checked on every call, not only at bind time."""
        for label, borrow in {
                "toolkit getter": lambda env, other: setattr(
                    env.tools, "get_db_hash", other.tools.get_db_hash),
                "environment getter": lambda env, other: setattr(
                    env, "get_db_hash", other.get_db_hash),
                "pay method": lambda env, other: setattr(
                    env.tools, "pay_delivery_order", other.tools.pay_delivery_order),
                "helper": lambda env, other: setattr(
                    env.tools, "_get_delivery_order", other.tools._get_delivery_order),
        }.items():
            with self.subTest(borrowed=label):
                env = self.chain_environment()
                other = self.chain_environment()
                bindings = environment_bindings(env)
                self.assertTrue(bindings["pay_delivery_order"].write_precondition)
                borrow(env, other)
                bridge = self.make_bridge(env)
                with self.assertRaises(BridgeBlocked):
                    bridge._execute(
                        {"tool_call_id": "binding-swap", "name": "pay_delivery_order",
                         "arguments": dumps({"order_id": HALLUCINATED_ORDER})}, bindings)
                self.assertEqual(
                    [e for e in bridge.trace if e["kind"] == "client_tool_result"], [])


if __name__ == "__main__":
    unittest.main()
