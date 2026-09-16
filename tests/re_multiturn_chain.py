"""Full continuous t4->t12 (18 phases) offline chain over the REAL fixed data.

This module builds a complete, internally consistent capture of the production
multi-turn driver running the WHOLE nine-task pair, for BOTH protocols:

* 0.2 (`ae-cloud-re-multiturn-0.2`): the production candidate. A real reviewed
  compatibility manifest is generated from a locally rebuilt patched Letta
  checkout (pinned baseline + the reviewed patch + the reviewed shim), and a real
  service-shaped load receipt is written by the unchanged bootstrap gate. The
  manifest is marked `applied_to_live_server: false`, so this is explicitly NOT
  a claim that a service process loaded the patch.

* 0.1 (`ae-cloud-re-multiturn-0.1`): the sealed protocol, for the same chain.

Every task's environment is the REAL pinned Vita environment for that task, and
every tool call the fixture produces is a REAL call executed against it: each
delivery task creates AND pays an order, each instore task searches and creates
AND pays an order, and both arms perform a real `memory_update` in the history
phase of EVERY task (R publishes it with a verified PATCH, E returns an erratum),
so cross-task memory, E's accumulated corrections and the domain switches really
happen - this is not nine empty rounds.

Only model/user/judge REPLIES are fixtures. No network, no service, no model.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.append(str(ROOT))

import test_ae_cloud_re_multiturn as t  # noqa: E402
from test_ae_cloud_input_audit import rewrite_records  # noqa: E402

ORIGINAL_CONFIG = ROOT / "configs/ae-01__re-multiturn__siliconflow.original-candidate.json"
PILOT_CONFIG = ROOT / "configs/ae-01__re-multiturn__siliconflow.pilot-t4-t5-candidate.json"
UNSEALED_CONFIG = ROOT / "configs/ae-01__re-multiturn__siliconflow.unsealed-candidate.json"
VERIFY = ROOT / "deployment-assets/letta-multicall/verify_deployment.py"


def workspace():
    """The extraction root holding the trusted archives' contents."""
    return Path(os.environ.get("AE_VERIFY_ROOT", ROOT / ".ae-verify-src"))


def pinned_letta():
    return workspace() / "letta-v1"


def patched_letta():
    return workspace() / "letta-multicall-patch/letta-v1"


def rebuild_patched_checkout():
    """Rebuild the reviewed patched checkout from the pinned baseline + patch.

    The baseline comes from the trusted archive extraction and is hash-checked
    against `ae_multicall.PATCH_BASELINE` by the production generator itself. The
    shim is copied from the reviewed `deployment-assets` source. Nothing is
    downloaded, invented or relaxed.
    """
    import shutil
    import subprocess
    import ae_multicall
    baseline, candidate = pinned_letta(), patched_letta()
    if not baseline.is_dir():
        raise RuntimeError(f"the pinned Letta baseline is absent: {baseline}")
    for name, expected in ae_multicall.PATCH_BASELINE.items():
        if expected is None:
            continue
        target = baseline / name
        if not target.is_file():
            raise RuntimeError(f"the pinned baseline is missing {name}")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"the pinned baseline {name} is not the pinned upstream file")
    if candidate.exists():
        shutil.rmtree(candidate)
    shutil.copytree(baseline, candidate)
    patch = ROOT / "deployment-assets/letta-multicall/ae-multicall-letta.patch"
    applied = subprocess.run(["git", "apply", str(patch)], cwd=candidate,
                             capture_output=True, text=True)
    if applied.returncode:
        raise RuntimeError("the reviewed patch did not apply: "
                           + (applied.stderr.strip() or applied.stdout.strip())[:200])
    shim = (ROOT / "deployment-assets/letta-multicall/files/letta/helpers"
            / "ae_multicall_compat.py")
    shutil.copy(shim, candidate / "letta/helpers/ae_multicall_compat.py")
    digests = {name: hashlib.sha256((candidate / name).read_bytes()).hexdigest()
               for name in ae_multicall.REQUIRED_PATCHED_FILES}
    return {"baseline": str(baseline), "candidate": str(candidate),
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "shim_sha256": hashlib.sha256(shim.read_bytes()).hexdigest(),
            "patched_sha256": digests}


def build_manifest(out):
    """Run the PRODUCTION manifest generator over the rebuilt candidate tree."""
    spec = importlib.util.spec_from_file_location(
        "ae_write_manifest", ROOT / "deployment-assets/letta-multicall/write_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_manifest(patched=patched_letta(), pinned=pinned_letta(),
                                 project=ROOT, letta_checkout=patched_letta(),
                                 out=Path(out), applied_to_live_server=False)


def build_receipt(receipt_path, manifest_path, manifest):
    """Run the UNCHANGED bootstrap gate to write a service-shaped load receipt.

    `resolve_letta_sources` is given a `find_spec` that resolves inside the real
    rebuilt checkout, because this process must not import Letta. Every path and
    digest in the receipt still comes from hashing the real files on disk, and
    `applied_to_live_server` stays false: this is NOT proof that a service loaded
    the patch.
    """
    spec = importlib.util.spec_from_file_location(
        "ae_bootstrap", ROOT / "scripts/deployment/letta_bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    import ae_multicall as multicall
    checkout = patched_letta()
    mapping = {
        "letta.agents.letta_agent_v3": "letta/agents/letta_agent_v3.py",
        "letta.schemas.message": "letta/schemas/message.py",
        "letta.helpers.ae_multicall_compat": "letta/helpers/ae_multicall_compat.py",
    }

    def find(module_name, package=None):
        if module_name not in mapping:
            return None
        return type("Spec", (), {"origin": str(checkout / mapping[module_name])})()

    receipt = bootstrap.install_multicall_gate({
        "AE_LETTA_MULTICALL_PROFILE": multicall.PROFILE_VERSION,
        "AE_LETTA_MULTICALL_MANIFEST": str(manifest_path),
        "AE_LETTA_MULTICALL_MODULE_SHA256": multicall.live_module_sha(),
        "AE_LETTA_MULTICALL_SUPPORT": str(ROOT),
        "AE_LETTA_MULTICALL_LOAD_RECEIPT": str(receipt_path)}, find_spec=find)
    return receipt


class ChainProvider(t.MultiturnProvider):
    """A provider that drives EVERY task with REAL tool calls, per domain."""

    def __init__(self, proxy, opener, sample, session, *, memory_every_task=True,
                 second_update_task=None, user_texts=None, multicall=None,
                 mutation=None, mutation_task=None):
        super().__init__(proxy, opener, sample, session)
        # Under the sealed 0.1 protocol a batch of >1 provider calls is a hard
        # stop by design, so the fixture emits one call at a time there and a real
        # batch under the declared 0.2 profile.
        self.multicall = multicall
        self.memory_every_task = memory_every_task
        self.second_update_task = second_update_task
        self.user_texts = list(user_texts or [])
        self.user_text_index = 0

    def request(self, method, path, body=None):
        """Answer `/v1/models` as the PROVIDER the proxy is really pointed at.

        The sealed fixtures reached the Qwen catalog, so the base class hard-codes that
        model. A model-specific transport (DeepSeek) must be exercised with the wire
        model its own proxy config declares, otherwise the driver's catalog check would
        only ever prove the fixture's constant.
        """
        if method == "GET" and path == "/v1/models":
            self.proxy.dispatch(method, path, b"", "re-multiturn/catalog",
                                "agent_or_unknown")
            return {"data": [{"id": self.proxy.config.upstream_model or t.pair.MODEL}]}
        return super().request(method, path, body)

    # -- history phase -----------------------------------------------------
    def decide(self, session, new_input, tool_schemas):
        task_id, phase = self._task(new_input)
        if task_id is None or phase is None:
            return t.pair.chat_reply("stop", content="fixture final answer")
        arm = session["arm"]
        number = int(task_id.rsplit("_", 1)[-1])
        memory_calls = session.setdefault("memory_calls", {})
        tool_returns = any(m.get("type") == "tool_return" for m in new_input)
        if phase == "history":
            if self.memory_every_task and not memory_calls.get(task_id):
                memory_calls[task_id] = 1
                return self._memory_call(arm, task_id, number, 0)
            if (self.second_update_task == number and memory_calls.get(task_id) == 1):
                memory_calls[task_id] = 2
                return self._memory_call(arm, task_id, number, 1)
            return t.pair.chat_reply("stop", content=f"{task_id} history noted")
        if phase == "current_task":
            session.setdefault("native_stage", {})[task_id] = "start"
            return self._native_reply(session, task_id, tool_schemas)
        if tool_returns:
            stage = session.setdefault("native_stage", {}).get(task_id)
            if stage in ("create", "pay", "search", "shop"):
                return self._native_reply(session, task_id, tool_schemas)
        return t.pair.chat_reply("stop", content="fixture final answer")

    def _memory_call(self, arm, task_id, number, index):
        # Every task's update cites THIS task's own history material.
        evidence = f"t{number}/history/0"
        content = f"t{number} 记忆备注 {index}"
        return t.pair.chat_reply("tool_calls", tool_calls=[
            {"id": f"call-{arm}-{task_id}-memory-{index}", "type": "function",
             "function": {"name": "memory_update",
                          "arguments": t.dumps({"operation": "add", "fact_id": "",
                                                "category": "饮食偏好", "content": content,
                                                "evidence_ref": evidence})}}])

    # -- task phase --------------------------------------------------------
    def _native_reply(self, session, task_id, tool_schemas):
        names = {schema["name"] for schema in tool_schemas}
        declared = self.sample["private_tasks"][task_id]["environment"]
        stage = session.setdefault("native_stage", {})
        arm = session["arm"]
        current = stage.get(task_id)
        now = declared.get("time") or "2024-06-23 12:00:00"
        if current in ("start", "shop"):
            if "create_delivery_order" in names:
                return self._delivery_create(session, task_id, declared, now, arm)
            if "create_instore_product_order" in names:
                return self._instore_create(session, task_id, declared, arm)
            stage[task_id] = "done"
            return t.pair.chat_reply("stop", content="fixture final answer")
        # A follow-up after a real tool return: pay the order we really created.
        order_id = self._last_order_id(session)
        if order_id and "pay_delivery_order" in names:
            stage[task_id] = "done"
            return self._pay(arm, task_id, order_id, "pay_delivery_order")
        if order_id and "pay_instore_order" in names:
            stage[task_id] = "done"
            return self._pay(arm, task_id, order_id, "pay_instore_order")
        stage[task_id] = "done"
        return t.pair.chat_reply("stop", content="fixture final answer")

    def _delivery_create(self, session, task_id, declared, now, arm):
        stores = declared.get("stores") or {}
        store = next(iter(stores))
        product = stores[store]["products"][0]["product_id"]
        address = (self.sample["initial_profile"].get(t.WORK_ADDRESS_KEY)
                   or (declared.get("location") or [{}])[0].get("address"))
        stage = session.setdefault("native_stage", {})
        stage[task_id] = "create"
        return t.pair.chat_reply("tool_calls", tool_calls=[
            {"id": f"call-{arm}-{task_id}-create", "type": "function",
             "function": {"name": "create_delivery_order",
                          "arguments": t.dumps({
                              "user_id": declared.get("user_id") or "U000828",
                              "store_id": store, "product_ids": [product],
                              "product_cnts": [1], "address": address,
                              "dispatch_time": now, "attributes": ["规格: 7分糖"]})}}])

    def _instore_create(self, session, task_id, declared, arm):
        shops = declared.get("shops") or {}
        shop = next(iter(shops))
        product = shops[shop]["products"][0]["product_id"]
        stage = session.setdefault("native_stage", {})
        create = {"id": f"call-{arm}-{task_id}-create", "type": "function",
                  "function": {"name": "create_instore_product_order",
                               "arguments": t.dumps({
                                   "user_id": declared.get("user_id") or "U000828",
                                   "shop_id": shop, "product_id": product,
                                   "quantity": 1})}}
        if stage.get(task_id) == "shop":
            # The search already returned; the order is the next single call.
            stage[task_id] = "create"
            return t.pair.chat_reply("tool_calls", tool_calls=[create])
        if self.multicall is not None:
            # A real two-call batch under the declared 0.2 profile.
            stage[task_id] = "create"
            return t.pair.chat_reply("tool_calls", tool_calls=[
                {"id": f"call-{arm}-{task_id}-shop", "type": "function",
                 "function": {"name": "instore_shop_search_recommend",
                              "arguments": t.dumps({"keywords": ["按摩", "团购"]})}},
                create])
        stage[task_id] = "shop"
        return t.pair.chat_reply("tool_calls", tool_calls=[
            {"id": f"call-{arm}-{task_id}-shop", "type": "function",
             "function": {"name": "instore_shop_search_recommend",
                          "arguments": t.dumps({"keywords": ["按摩", "团购"]})}}])

    def _pay(self, arm, task_id, order_id, tool):
        return t.pair.chat_reply("tool_calls", tool_calls=[
            {"id": f"call-{arm}-{task_id}-pay", "type": "function",
             "function": {"name": tool, "arguments": t.dumps({"order_id": order_id})}}])

    @staticmethod
    def _last_order_id(session):
        """The order id out of the REAL create return, never invented."""
        for message in reversed(session.get("_last_input") or []):
            if message.get("type") != "tool_return":
                continue
            for returned in message.get("tool_returns") or []:
                if returned.get("status") != "success":
                    continue
                order_id = t.pair.real_order_id(returned.get("tool_return"))
                if order_id:
                    return order_id
        return None


class MutatingChainProvider(ChainProvider):
    """A chain whose SEMANTICS are broken while the transport stays consistent.

    Every mutation rewrites the REAL outgoing provider body (or, for the return
    case, the REAL executed return) inside the fixture that produces it, so the
    capture is a genuine, internally consistent capture of a run that did the
    wrong thing - never a hand-edited journal. That is what lets a negative case
    prove a TARGET gate refuses it rather than the shared transport gate.

    `mutation` selects one bounded behaviour:
      * omit_history          - a phase's request omits its original history batch
      * alter_update_return   - the executed memory_update return names a wrong id
      * runtime_user_inject   - a runtime_user wire carries non-native text
      * swap_history_batches  - two tasks' history batches are exchanged
      * extra_cloud_call       - the LAST journaled cloud call is duplicated after
                                 the capture's own close, so the transport chain
                                 stays valid and ONLY the consumption accounting
                                 can refuse the orphan
      * auxiliary_role_mismatch- an auxiliary call claims the other role
      * swap_judge_windows     - two of a task's judge replies are exchanged
      * context_reset          - a later phase restarts from an empty context
                                 (realised on the driver record, which is the only
                                 place a client POST can express it)
    """

    def __init__(self, *args, mutation=None, mutation_task=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mutation = mutation
        self.mutation_task = mutation_task
        # `request_mutator` runs before this POST's provider body is recorded and
        # dispatched, so a mutated POST is a genuine, internally consistent
        # capture of a request that really carried the mutated material.
        self.request_mutator = self._mutate_body
        self.reply_mutator = self._mutate_reply
        self.role_mutator = self._mutate_role


    def _is_target(self, task_id):
        return self.mutation_task is None or task_id == self.mutation_task

    @staticmethod
    def envelope_of(message):
        """The public envelope of a submitted user message, or None.

        The public envelopes are COMPACT JSON (no space after `:`), so a textual
        pattern would silently match nothing; the envelope is parsed.
        """
        if not (isinstance(message, dict) and message.get("role") == "user"):
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None
        try:
            envelope = json.loads(content)
        except (TypeError, ValueError):
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get("source") not in ("dataset_history/material", "current_task",
                                          "runtime_user"):
            return None
        return envelope

    @classmethod
    def envelope_number(cls, message):
        """The task number a public envelope belongs to, or None."""
        envelope = cls.envelope_of(message)
        if envelope is None:
            return None
        if envelope.get("source") == "dataset_history/material":
            number = envelope.get("task_number")
            return number if type(number) is int else None
        subtask_id = envelope.get("subtask_id")
        if isinstance(subtask_id, str) and subtask_id.startswith("sub_U000828_"):
            tail = subtask_id.rsplit("_", 1)[-1]
            return int(tail) if tail.isdigit() else None
        # A runtime_user envelope carries only its own ref (`tN/user/k`).
        ref = envelope.get("ref")
        if isinstance(ref, str) and ref.startswith("t") and "/user/" in ref:
            turn = ref[1:].split("/", 1)[0]
            return int(turn) if turn.isdigit() else None
        return None

    #: Backwards-compatible name used inside the mutation helpers.
    _envelope_number = envelope_number

    def chat(self, session, new_input, tool_schemas):
        task_id, phase = self._task(new_input)
        self._current = (task_id, phase)

        if self.mutation == "swap_history_batches" and phase == "history" \
                and self._is_target(task_id):
            # Two tasks' history batches are exchanged: each stays a legal,
            # complete batch, but under the wrong task identity.
            session.setdefault("_swap_done", False)
            reply = super().chat(session, new_input, tool_schemas)
            return reply
        return super().chat(session, new_input, tool_schemas)

    def _mutate_role(self, role):
        """Change ONE auxiliary call's captured dispatch role.

        Only the captured role is changed (the native record keeps its own), so the
        run still completes normally and only the role correspondence is broken.
        The skewed call is a LATE ancillary call whose native record uses the same
        request messages, so it is the LAST call of its role rather than, say, the
        evaluator call whose result the driver still needs.
        """
        if self.mutation != "auxiliary_role_mismatch" or role != "user_simulator":
            return role
        counter = self.session.get("_us_count", 0) + 1
        self.session["_us_count"] = counter
        if counter != 2:
            return role
        return "evaluator"

    def _mutate_reply(self, provider, body, reply):
        """Alter a raw provider reply (the model output the bridge will execute)."""
        task_id, phase = self._current
        if provider.mutation == "alter_update_return" and phase == "history" \
                and provider._is_target(task_id):
            provider._alter_reply_update(reply, body)
        return reply

    def chain_post_run(self, journal_path):
        """Capture-level mutations applied after the run, before the audit.

        The duplicated orphan is written AFTER the capture's own close and with
        every timestamp preserved (only the appended records get a later one), so
        the transport chain stays internally valid and only the consumption
        accounting can refuse it - which is exactly the gate this probe targets.
        """
        if self.mutation != "extra_cloud_call":
            return
        from datetime import datetime, timedelta
        path = Path(journal_path)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        close_index = next(i for i, row in enumerate(rows)
                           if row.get("kind") == "cloud_close")
        starts = [i for i, row in enumerate(rows) if row.get("kind") == "client_request"]
        group = rows[starts[-1]:close_index]
        need = [row for row in rows if row.get("kind") in
                ("cloud_open", "client_request", "normalized_request", "pace_wait",
                 "upstream_request", "upstream_response", "cloud_summary", "cloud_close")]
        # Insert the orphan group AFTER the last real group but BEFORE the close,
        # with timestamps that keep the journal clock monotonic, so the transport
        # walk itself stays satisfied and only the consumption accounting fails.
        last_timestamp = max(str(row.get("timestamp")) for row in rows[:close_index])
        base = datetime.fromisoformat(last_timestamp)
        duplicate = []
        for offset, row in enumerate(group):
            clone = json.loads(json.dumps(row))
            clone["request_id"] = "orphan-" + str(row.get("request_id"))[:8]
            clone["timestamp"] = (base + timedelta(microseconds=offset + 1)).isoformat()
            duplicate.append(clone)
        rows = rows[:close_index] + duplicate + rows[close_index:]
        for index, row in enumerate(rows):
            row["sequence"] = index
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")

    def chain_result_mutator(self, result):
        """Driver-record mutation for the self-consistent-but-unbacked case.

        `alter_update_return` rewrites the run's own verified ledger so that the
        fact id it claims the update resolved to is one the executed
        memory_update return never produced. The capture is a real capture of a
        run whose ledger and its executed return disagree - the exact defect a
        check that only trusts the ledger would miss.
        """
        if self.mutation != "alter_update_return":
            return
        target = self.mutation_task
        for arm in t.ARMS:
            for task in result["arms"][arm]["tasks"]:
                if target is not None and task["subtask_id"] != target:
                    continue
                for write in task.get("memory_writes_this_task_expected") or []:
                    write["resolved"]["fact_id"] = "p999"

    def _swap_auxiliary_replies(self, session, reply):
        """Hold one auxiliary reply and use the NEXT one in its place."""
        from copy import deepcopy as _deepcopy
        held = session.setdefault("_aux_held", [])
        held.append(_deepcopy(reply))
        if len(held) >= 2:
            first, second = held[0], held[1]
            session["_aux_held"] = held[2:]
            return first
        return reply

    def _alter_reply_update(self, reply, body):
        """Change the fact_id the model's own memory_update return will resolve to.

        The reply is the real provider output shape, so the bridge really executes
        the altered call and the run's ledger really disagrees with it.
        """
        for choice in reply.get("choices") or []:
            message = choice.get("message") or {}
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("name") != "memory_update":
                    continue
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    continue
                if isinstance(args, dict):
                    args["fact_id"] = "p007" if not args.get("fact_id") else args["fact_id"]
                    # An `add` with a non-empty fact_id is rejected by design, so
                    # turn it into a replace of an existing fact instead.
                    args["operation"] = "replace"
                    args["category"] = "饮食偏好"
                    function["arguments"] = json.dumps(args, ensure_ascii=False)

    def mutate_letta_post(self, post_index, body):
        """Rewrite one SUBMITTED Letta POST body (the object under audit).

        The task of the POST is derived from the body's own public envelope, so
        the mutation is applied to the right phase without relying on a counter
        that a retry could desynchronise.
        """
        task_numbers = set()
        for message in body.get("messages") or []:
            number = self._envelope_number(message)
            if number is not None:
                task_numbers.add(number)
        if not task_numbers:
            return body
        number = sorted(task_numbers)[-1]
        if self.mutation_task is not None:
            target = int(self.mutation_task.rsplit("_", 1)[-1])
            if number != target:
                return body
        def is_history(message):
            envelope = self.envelope_of(message)
            return (envelope is not None
                    and envelope.get("source") == "dataset_history/material"
                    and envelope.get("task_number") == number)

        if self.mutation == "omit_history":
            for message in body["messages"]:
                if not is_history(message):
                    continue
                envelope = json.loads(message["content"])
                envelope["records"] = []
                message["content"] = json.dumps(envelope, ensure_ascii=False)
                break
        elif self.mutation == "swap_history_batches":
            other = 4 if number != 4 else 5
            for message in body["messages"]:
                if not is_history(message):
                    continue
                envelope = json.loads(message["content"])
                envelope["task_number"] = other
                message["content"] = json.dumps(envelope, ensure_ascii=False)
                break
        elif self.mutation == "runtime_user_inject":
            for message in body["messages"]:
                envelope = self.envelope_of(message)
                if envelope is None or envelope.get("source") != "runtime_user":
                    continue
                envelope["content"] = "不是本次原生模拟器的回复"
                message["content"] = json.dumps(envelope, ensure_ascii=False)
                break
        return body

    def _mutate_body(self, provider, body):
        task_id, phase = self._current
        if self.mutation == "omit_history" and phase == "history" \
                and self._is_target(task_id):
            # The envelope stays legal and in place; its RECORDS are emptied, so
            # the task's original history material never reaches the model.
            number = int(task_id.rsplit("_", 1)[-1])
            for message in body["messages"]:
                if self._envelope_number(message) != number:
                    continue
                envelope = json.loads(message["content"])
                envelope["records"] = []
                message["content"] = json.dumps(envelope, ensure_ascii=False)
                break
        elif self.mutation == "runtime_user_inject" and phase == "runtime_user" \
                and self._is_target(task_id):
            for message in reversed(body["messages"]):
                if isinstance(message, dict) and message.get("role") == "user":
                    try:
                        envelope = json.loads(message["content"])
                    except (TypeError, ValueError):
                        break
                    if isinstance(envelope, dict) and envelope.get("source") == "runtime_user":
                        envelope["content"] = "不是本次原生模拟器的回复"
                        message["content"] = json.dumps(envelope, ensure_ascii=False)
                    break
        elif self.mutation == "swap_history_batches" and phase == "history" \
                and self._is_target(task_id):
            number = int(task_id.rsplit("_", 1)[-1])
            other = 4 if number != 4 else 5
            for message in body["messages"]:
                if not (isinstance(message, dict) and message.get("role") == "user"
                        and isinstance(message.get("content"), str)
                        and f'"task_number": {number}' in message["content"]):
                    continue
                envelope = json.loads(message["content"])
                envelope["task_number"] = other
                message["content"] = json.dumps(envelope, ensure_ascii=False)
        return body

    def _alter_last_update_return(self, session):
        """Make the executed memory_update return disagree with the ledger."""
        for message in reversed(session.get("_last_input") or []):
            if message.get("type") != "tool_return":
                continue
            for returned in message.get("tool_returns") or []:
                if returned.get("status") != "success":
                    continue
                try:
                    payload = json.loads(returned.get("tool_return") or "{}")
                except ValueError:
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("update"), dict):
                    payload["update"]["fact_id"] = "p999"
                    returned["tool_return"] = json.dumps(payload, ensure_ascii=False)
                    return


def _scoring_entries(result, task_id):
    """Every phase record whose task id is the target, for both arms."""
    found = []
    for arm in t.ARMS:
        for task in result["arms"][arm]["tasks"]:
            if task["subtask_id"] == task_id:
                found.append(task)
    return found


def _carried_state_of(call):
    """The `<current_rubrics>` items one evaluator call carried, and their span."""
    text = call["request"]["messages"][1]["content"]
    match = re.search(r"<current_rubrics>\s*(.*?)\s*</current_rubrics>", text, re.S)
    assert match is not None, "the recorded judge prompt carries no rubric section"
    items = json.loads(match.group(1))
    return text, match, items


def _rewrite_carried_state(call, items, text, match):
    """Write a mutated rubric section back into the prompt AND its record.

    A real capture sends exactly one prompt, so the mutation has to appear in the
    request and in `window_evaluations.user_prompt` together; otherwise the
    window-evaluation cross-check would refuse it instead of the scoring rule.
    """
    changed = text[:match.start(1)] + json.dumps(items, ensure_ascii=False) + text[match.end(1):]
    call["request"]["messages"][1]["content"] = changed
    return changed


def _mutate_scoring(result, task_id, mutation):
    """Driver-record scoring mutations, applied before the result is written.

    These deliberately break the SCORING evidence only: the transport chain, the
    agent wire and the auxiliary pairing are untouched, so a refusal can only come
    from the scoring gate. Each case is a deviation a real capture could show.
    """
    entries = _scoring_entries(result, task_id)
    assert entries, f"no phase record for {task_id}"
    for task in entries:
        native = task.get("native_calls_this_task") or []
        evaluators = [call for call in native if call.get("role") == "evaluator"]
        reward = (task.get("judge") or {}).get("reward_info") or {}
        evaluations = reward.get("window_evaluations") or []
        if mutation == "rubric_text_change":
            for index, call in enumerate(evaluators):
                content = call["response"]["content"]
                parsed = json.loads(content.split("```json", 1)[1].rsplit("```", 1)[0])
                parsed[0]["rubric"] = "被篡改的评分要求文本"
                call["response"]["content"] = ("```json\n"
                                               + json.dumps(parsed, ensure_ascii=False)
                                               + "\n```")
                raw = call["response"].get("raw_data")
                if isinstance(raw, dict) and raw.get("choices"):
                    raw["choices"][0]["message"]["content"] = call["response"]["content"]
                # A real capture sends exactly one reply per window, so the recorded
                # window evaluation carries the SAME text; without this the mutation
                # would be refused by the prompt/reply cross-check instead of by the
                # rubric-echo rule under test (which the native-semantics mode skips).
                if index < len(evaluations):
                    evaluations[index]["assistant_message_content"] = \
                        call["response"]["content"]
        elif mutation == "final_met_flip":
            rubrics = reward.get("nl_rubrics")
            if rubrics:
                rubrics[0]["met"] = not rubrics[0]["met"]
        elif mutation == "final_missing":
            reward.pop("nl_rubrics", None)
        elif mutation == "final_empty":
            reward["nl_rubrics"] = []
        elif mutation == "window_drop":
            for call in evaluators[1:2]:
                native.remove(call)
        elif mutation == "window_duplicate":
            if evaluators:
                native.append(deepcopy(evaluators[0]))
        elif mutation == "window_order":
            # Swap the FIRST TWO evaluator calls, so the recorded order really
            # differs from the pinned expansion order (window 2 before window 1).
            if len(evaluators) >= 2:
                first_index = native.index(evaluators[0])
                second_index = native.index(evaluators[1])
                native[first_index], native[second_index] = (
                    native[second_index], native[first_index])
        elif mutation in ("window_final_flip", "window_final_justification"):
            # The state a LATER window carries must be the state the previous
            # window's own reply produced: flip its boolean, or rewrite the
            # justification it must carry forward.
            assert len(evaluators) >= 2, f"{task_id} has no second window to mutate"
            call = evaluators[1]
            text, match, items = _carried_state_of(call)
            assert items, "the second window carries no rubric state"
            if mutation == "window_final_flip":
                items[0]["meetExpectation"] = not items[0]["meetExpectation"]
            else:
                items[0]["justification"] = "被改写的承接理由"
            changed = _rewrite_carried_state(call, items, text, match)
            if len(evaluations) >= 2:
                evaluations[1]["user_prompt"] = changed
        elif mutation == "window_initial_change":
            # The FIRST window must carry the pinned initialiser's own state:
            # unmet, with its initial justification.
            assert evaluators, f"{task_id} has no window to mutate"
            call = evaluators[0]
            text, match, items = _carried_state_of(call)
            assert items, "the first window carries no rubric state"
            items[0]["justification"] = "被伪造的初始理由"
            changed = _rewrite_carried_state(call, items, text, match)
            if evaluations:
                evaluations[0]["user_prompt"] = changed
        else:
            raise ValueError(f"unknown scoring mutation: {mutation}")
        # A real run records the SAME evaluation three times: inside its completion, as
        # the last evaluation, and in the judge record. A mutation that changed the judge
        # record alone would be refused by that copy cross-check instead of by the scoring
        # rule under test, so the snapshot copies follow the mutated record.
        snapshot = task.get("native_snapshot") or {}
        for entry in snapshot.get("completed") or []:
            if entry.get("subtask_id") == task.get("subtask_id"):
                entry["reward_info"] = deepcopy(reward)
        if isinstance(snapshot.get("last_evaluation"), dict):
            snapshot["last_evaluation"]["reward_info"] = deepcopy(reward)


def build_full_chain(tmp_path, *, unsealed=False, judge_fail_tasks=(),
                     memory_every_task=True, second_update_task=None,
                     environment_builder=None, receipt=True, mutate=None,
                     provider_class=None, mutation=None, mutation_task=None,
                     user_texts=None, result_mutator=None, user_script=None,
                     judge_windows=True, user_script_tasks=(),
                     user_script_stop_after=None, user_script_map=None,
                     window_met=True, scoring_mutation=None,
                     scoring_task="sub_U000828_4", config_path=None,
                     patch_stack_manifest=None, patch_stack_load_receipt=None,
                     judge_echo_variant_windows=()):
    """Build one full chain.

    `user_script` is either a list of user texts (applied per arm, in order) or a
    callable taking the arm name and returning that list. `mutate_events` and
    `result_mutator` are only used by negative fixtures.
    """
    """Build one complete 18-phase capture over the REAL fixed dataset."""
    run_dir = Path(tmp_path) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(config_path) if config_path else (
        UNSEALED_CONFIG if unsealed else ORIGINAL_CONFIG)
    declared = json.loads(config_path.read_text(encoding="utf-8"))
    # A 0.4 declaration needs the exploratory opt-in the real CLI passes as a FLAG; the
    # fixture stands in for that flag from the config's own declared protocol, using the
    # provider module's literal rather than a copy, so the two can never drift apart.
    from ae_deepseek_re_transport import NON_GUARANTEE_OPTION

    _option = (declared.get("_exploratory_capacity_option")
               or (NON_GUARANTEE_OPTION
                   if declared.get("exploratory_capacity_protocol") else None))
    declared.pop("_exploratory_capacity_option", None)
    config = t.validate_config(declared, exploratory_capacity_option=_option)
    sample = t.real_sample(t.END_TURN)
    if sample is None:
        raise RuntimeError("the fixed dataset is not available")
    sample = mutate(deepcopy(sample)) if mutate else sample
    plan = t.build_plan(config, sample, code_files=t.multiturn_code_files(ROOT))
    # The production CLI builds the plan from the CONFIG FILE it was given, so the
    # plan's `config` IS that file's contents. The fixture does the same, which is what
    # lets a declaration digest be compared between a plan and a proxy journal.
    plan["config"] = {key: value for key, value
                      in json.loads(config_path.read_text(encoding="utf-8")).items()
                      if key != "_exploratory_capacity_option"}
    cli = t.pair.load_cli_module("ae_01_cloud_re_multiturn")

    manifest_path = receipt_path = None
    if config.get("multicall_profile") is not None:
        # Rebuild the reviewed patched checkout from the pinned baseline + patch
        # and generate the REAL manifest + service-shaped receipt. The manifest is
        # marked `applied_to_live_server: false`; this is offline evidence, not a
        # claim that a service loaded the patch.
        rebuild_patched_checkout()
        manifest_path = Path(tmp_path) / "ae-multicall-manifest.json"
        build_manifest(manifest_path)
        if receipt:
            receipt_path = Path(tmp_path) / "multicall-load-receipt.json"
            build_receipt(receipt_path, manifest_path,
                          json.loads(manifest_path.read_text(encoding="utf-8")))
    vita_source = Path(os.environ.get("AE_VITA_SOURCE", t.VITA_SOURCE))
    prov = cli.provenance(config, config_path, manifest_path, receipt_path,
                          vita_source=vita_source,
                          patch_stack_manifest=patch_stack_manifest,
                          patch_stack_load_receipt=patch_stack_load_receipt)
    plan["provenance"] = prov
    if config.get("capacity") is not None:
        # The production CLI records this before any output directory exists; the
        # fixture records the same shape so the audit's capacity gate sees a real
        # run record rather than a synthetic one.
        plan["capacity_decision"] = {
            "capacity_declared": True, "run_allowed": False,
            "context_window": config["capacity"]["context_window"],
            "window_source": config["capacity"]["window_source"],
            "verification": config["capacity"]["verification"],
            "endpoint_measured_tokens":
                config["capacity"]["service_limit"]["endpoint_measured_tokens"],
            "open_item": "endpoint capacity unverified",
            "no_compaction": config["capacity"]["no_compaction"],
            "count_basis": list(config["capacity"]["count_basis"])}
    t.write_json(run_dir / "plan.json", plan)
    # The run-local Vita model configuration the PRODUCTION driver writes next to its
    # plan. The audit establishes it before it imports the pinned package (the pinned
    # `vita.config` otherwise falls back to the checkout's example config), so the fixture
    # writes the same file through the same production helper and points the variable at
    # IT. A `vita.*` package left over from another fixture's configuration is dropped, the
    # way a fresh driver process would start from; the audit refuses a package whose
    # loaded configuration is not this run's declaration.
    from ae_vita import native_model_config
    vita_models = run_dir / "vita-models.json"
    t.write_json(vita_models, native_model_config(plan["config"]))
    os.environ["VITA_MODEL_CONFIG_PATH"] = str(vita_models)
    for _name in [name for name in sys.modules
                  if name == "vita" or name.startswith("vita.")]:
        del sys.modules[_name]

    # The patched client keeps tool-call ids whole only while the declared
    # profile is in the environment; the fixture must be the same client.
    profile_declared = config.get("multicall_profile") is not None
    previous_profile = os.environ.get(t.MULTICALL_PROFILE_ENV)
    if profile_declared:
        os.environ[t.MULTICALL_PROFILE_ENV] = config["multicall_profile"]["version"]
    clock = t.pair.FakeClock()
    from ae_cloud_proxy import (DEEPSEEK_ORIGIN, DEEPSEEK_PROFILE, CloudAuditProxy,
                                CloudConfig)
    # The proxy must speak the profile the RUN declares. A 0.4 run is served by the
    # DeepSeek transport (its own origin, wire model and declared byte budget); every
    # other candidate keeps the reviewed Qwen transport config byte-for-byte.
    if config.get("transport_profile") == DEEPSEEK_PROFILE:
        proxy_cloud = CloudConfig(
            model=config["expected_model"],
            # The transport's output ceiling is the AUXILIARY limit: an agent call is
            # pinned at 2048 by the driver and the audit, while the auxiliary roles do
            # reach 4096, so a proxy ceiling of 2048 would refuse them.
            max_output_tokens=config["auxiliary_output_tokens"],
            max_request_bytes=config["max_request_bytes"],
            max_response_bytes=config["max_response_bytes"],
            max_requests=config["max_requests"], io_timeout_seconds=180.0,
            upstream_origin=DEEPSEEK_ORIGIN, profile=DEEPSEEK_PROFILE,
            upstream_model=config["upstream_model"],
            declared_request_byte_budget=config["capacity"]["max_request_bytes"],
            pace_seconds=config["pacing"]["min_interval_seconds"])
        proxy_cloud.validate()
    else:
        proxy_cloud = CloudConfig(**json.loads(t.TRANSPORT_CONFIG.read_text()))
    proxy = CloudAuditProxy(proxy_cloud, Path(tmp_path) / "proxy.private.jsonl",
                            api_key=t.KEY, clock=clock.monotonic, sleep=clock.sleep)
    opener = t.pair.ChatOpener()
    proxy.opener = opener
    if proxy_cloud.upstream_model:
        # The provider's catalog is the one THIS transport reaches: a model-specific proxy
        # must not be served another provider's model list, or its own run would be
        # audited against a catalog it could never have received.
        opener.catalog = json.dumps(
            {"object": "list", "data": [{"id": proxy_cloud.upstream_model}]}).encode()
    # A 0.4 candidate is served by the MODEL-SPECIFIC byte gate, and the driver arms it
    # itself: it is the driver that owns the declaration, and a proxy armed twice would
    # leave two arming rows in the journal - which the audit refuses, because a capture
    # must show exactly which ONE policy it ran under. The proxy is constructed with the
    # declared byte budget, so the gap between the two arming paths is closed by the
    # declaration, not by the fixture.
    provider_post_mutator = None
    if mutation:
        provider_holder = {}

        def provider_post_mutator(session_double, post_index, body):  # noqa: F811
            provider = provider_holder.get("provider")
            if provider is None:
                return body
            return provider.mutate_letta_post(post_index, body)

    session = t.MutatingLettaPair(
        config=config, update_modes={arm: "none" for arm in t.ARMS},
        post_mutator=provider_post_mutator)
    context, environments = (environment_builder or t.real_environment_factory)(sample)
    try:
        provider = (provider_class or ChainProvider)(
            proxy, opener, sample, session,
            memory_every_task=memory_every_task,
            second_update_task=second_update_task,
            multicall=config.get("multicall_profile"),
            mutation=mutation, mutation_task=mutation_task,
            user_texts=user_texts)
        if mutation:
            provider_holder["provider"] = provider
            if getattr(provider, "role_mutator", None) is not None:
                pass
        session.provider = provider
        recorder = t.pair.RecorderTransport(session, run_dir / "letta-http.jsonl")
        natives = {}

        def runtime_factory(arm):
            script = user_script(arm) if callable(user_script) else user_script
            native = t.MultiturnNative(arm, proxy, opener, environments,
                                       judge_fail_tasks=judge_fail_tasks,
                                       user_script=script,
                                       judge_windows=judge_windows, sample=sample,
                                       judge_echo_variant_windows=(
                                           judge_echo_variant_windows))
            native.user_script_tasks = set(user_script_tasks)
            native.user_script_map = dict(user_script_map or {})
            # A legal LOW score: every window decides `met=False`, so the pinned
            # aggregation yields reward 0.0 and the audit must still pass it.
            native.window_met = window_met
            natives[arm] = native
            return native

        events = []

        def emit(event):
            events.append(dict(deepcopy(event), timestamp=t.pair.now()))

        result = t.execute_re_multiturn(config, sample, model_transport=provider,
                                        letta_transport=recorder,
                                        runtime_factory=runtime_factory, emit=emit)
        # A driver-record mutation: the capture is a real capture of a run whose
        # own internal records disagree, which is exactly the class of defect a
        # check that trusts a single record would miss.
        if result_mutator is not None:
            result_mutator(result)
        if hasattr(provider, "chain_result_mutator"):
            provider.chain_result_mutator(result)
        if scoring_mutation is not None:
            _mutate_scoring(result, scoring_task, scoring_mutation)
        recorder.close()
        proxy.close()
        result["provenance"] = cli.provenance(config, config_path, manifest_path,
                                              receipt_path, vita_source=vita_source)
        if config.get("capacity") is not None:
            result["capacity_decision"] = plan["capacity_decision"]
        t.write_json(run_dir / "result.json", result)
        t.write_records(run_dir / "events.jsonl", events)
        t.pair.normalize_records(run_dir / "events.jsonl", events)
    finally:
        context.__exit__(None, None, None)
        if mutation and hasattr(provider, "chain_post_run"):
            # Runs after the proxy has closed, so the journal is sealed and the
            # mutation operates on the final capture.
            provider.chain_post_run(Path(tmp_path) / "proxy.private.jsonl")
        if profile_declared:
            if previous_profile is None:
                os.environ.pop(t.MULTICALL_PROFILE_ENV, None)
            else:
                os.environ[t.MULTICALL_PROFILE_ENV] = previous_profile
    return {"run_dir": run_dir, "journal": Path(tmp_path) / "proxy.private.jsonl",
            "config_path": config_path,
            "declared_config": (config_path if config_path.is_relative_to(tmp_path) else None),
            "result": result, "events": events,
            "session": session, "natives": natives, "plan": plan, "sample": sample,
            "manifest_path": manifest_path, "receipt_path": receipt_path}


def audit_of(fixture, **kwargs):
    """Audit a built chain exactly as the production CLI would.

    The DECLARED config file is passed whenever the fixture used one, so the audit's
    "declared_config_not_the_run_config" check compares against the same bytes the
    run really used.
    """
    from ae_cloud_re_multiturn_input_audit import audit_re_multiturn_inputs
    kwargs.setdefault("dataset_path", t.DATASET)
    declared = fixture.get("declared_config") or fixture["config_path"]
    return audit_re_multiturn_inputs(fixture["run_dir"], fixture["journal"],
                                     config_path=declared, **kwargs)


def _main():
    results = {}
    for label, unsealed in (("sealed-0.1", True), ("declared-0.2", False)):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fx = build_full_chain(tmp, unsealed=unsealed)
            except Exception as exc:
                results[label] = {"driver": "BUILD_FAILED", "error": f"{type(exc).__name__}: {exc}"}
                continue
            report = audit_of(fx)
            results[label] = {
                "driver": fx["result"]["status"],
                "audit": report["status"],
                "reasons": report["invalid_reasons"],
                "phases": [len(fx["result"]["arms"]["rewrite"]["tasks"]),
                           len(fx["result"]["arms"]["erratum"]["tasks"])],
                "cloud_calls": report["cloud_calls"],
                "protocol": (fx["result"]["config"]["schema_version"]),
            }
            print(label, json.dumps(results[label], ensure_ascii=False)[:400])
    return results


if __name__ == "__main__":
    _main()
