"""AE no-compaction policy: the one place the policy version is written down.

The AE multi-turn research protocol keeps each arm's accumulated history intact:
automatic (lossy) history compaction is not an accepted transformation of the
transcript. A candidate therefore declares the policy by tagging its agents, and
the pinned service stops a compaction attempt where it would begin.

Nothing here changes the default behaviour: with no tag, `compact_messages` runs
exactly as upstream. With the tag, compaction is refused before any summariser
model is called and before the in-context message set is touched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Optional

#: The policy version the AE driver puts on its agents as the tag below.
NO_COMPACTION_POLICY = "ae-no-compaction-1"

#: Agent tag carrying that policy. `<name>:<version>` so a future policy cannot be
#: mistaken for this one.
NO_COMPACTION_TAG = "ae-no-compaction:1"

#: The MODEL-SPECIFIC branch for a provider with no verified tokenizer (the DeepSeek
#: multi-turn candidate). It is a separate policy, not a relaxation of the token gate:
#:
#: * it is only reachable when the request declares this basis explicitly;
#: * the token count is NOT required - and is never faked - so a provider without a
#:   tokenizer can still be sent to;
#: * the operational bound becomes the request's own measured BYTES against a DECLARED
#:   budget, and a request over that budget is refused BEFORE anything is sent;
#: * the decision says `capacity_is_a_guarantee = False`, because a byte budget proves
#:   nothing about the provider's token window.
#:
#: The Qwen token basis is unchanged: with a token count, the branch below is not taken.
BYTE_GATE_COUNT_BASIS = "no_token_count_byte_gate_only"

# NOTE: there is deliberately NO byte-length or bytes-per-token fallback here. A
# ratio can under-estimate (10000 ASCII digits are 10000 official tokens but only
# ~4167 at 2.4 bytes/token), which would let an over-capacity request through, so an
# uncounted request is refused instead.


class NoCompactionPolicyError(RuntimeError):
    """Raised when a compaction is attempted under the declared no-compaction policy.

    It is deliberately not a summary failure: the step must stop with the raw state
    intact, and a caller must never retry it through another compaction path.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(
            "AE-NO-COMPACTION: the declared policy refused a compaction attempt "
            f"({reason}); history is retained and the step is stopped before any "
            "summariser call or message-set change"
        )


def no_compaction_declared(agent_tags: Optional[Iterable[str]]) -> bool:
    """Whether the agent's own tags declare the no-compaction policy.

    An unrecognised `ae-no-compaction:*` value is NOT treated as "declared": the
    caller refuses an unknown policy version instead of silently allowing the
    upstream default compaction to run under an AE tag.
    """
    for tag in agent_tags or []:
        if isinstance(tag, str) and tag.startswith("ae-no-compaction:"):
            return tag == NO_COMPACTION_TAG
    return False


def unknown_no_compaction_policy(agent_tags: Optional[Iterable[str]]) -> Optional[str]:
    """The unrecognised policy tag, if the agent carries one."""
    for tag in agent_tags or []:
        if isinstance(tag, str) and tag.startswith("ae-no-compaction:"):
            if tag != NO_COMPACTION_TAG:
                return tag
    return None


def require_no_compaction(agent_tags: Optional[Iterable[str]], where: str) -> None:
    """Abort when the declared policy forbids this compaction, or when it is unknown."""
    unknown = unknown_no_compaction_policy(agent_tags)
    if unknown is not None:
        raise NoCompactionPolicyError(f"unknown policy tag {unknown!r} at {where}")
    if no_compaction_declared(agent_tags):
        raise NoCompactionPolicyError(f"declared policy reached {where}")


def policy_state(agent_tags: Optional[Iterable[str]]) -> dict:
    """The declared policy's state for one agent, with unknown versions refused.

    Three outcomes only: `off` (no AE policy tag at all), `on` (the reviewed tag), and
    `unknown` (an `ae-no-compaction:*` tag this build does not know). `unknown` is a
    refusal everywhere - a request must never be sent because a policy version could
    not be recognised.
    """
    for tag in agent_tags or []:
        if isinstance(tag, str) and tag.startswith("ae-no-compaction:"):
            if tag == NO_COMPACTION_TAG:
                return {"state": "on", "tag": tag}
            return {"state": "unknown", "tag": tag}
    return {"state": "off", "tag": None}


def byte_gate_decision(*, agent_tags: Optional[Iterable[str]], request_bytes,
                       byte_budget, count_basis: Optional[str] = None,
                       count_source: Optional[str] = None,
                       declared_wire_model: Optional[str] = None,
                       request_wire_model: Optional[str] = None) -> dict:
    """The model-specific branch: bound a request by its OWN bytes, not by tokens.

    It exists so a provider WITHOUT a verified tokenizer is still bounded honestly. It
    never produces a token count, it never calls a summariser, and it refuses an
    undeclared basis, an unknown policy version, an unusable byte measurement or a
    request over the declared budget.
    """
    policy = policy_state(agent_tags)
    if policy["state"] == "off":
        return {"active": False, "allowed": True, "reason": "no AE capacity policy declared"}
    if policy["state"] == "unknown":
        return {"active": True, "allowed": False, "reason": "unknown_policy_version",
                "tag": policy["tag"]}
    if count_basis != BYTE_GATE_COUNT_BASIS:
        return {"active": True, "allowed": False, "reason": "unexpected_count_basis",
                "count_basis": count_basis}
    # The policy is MODEL-SPECIFIC: it may only bound the request whose WIRE model is the
    # one the policy was declared for. A different model must not be waved through by a
    # byte budget while the token gate it would normally face is skipped - that would be
    # a silent relaxation for every other model in the same process.
    if not isinstance(declared_wire_model, str) or not declared_wire_model:
        return {"active": True, "allowed": False, "reason": "byte_policy_declares_no_model"}
    if request_wire_model != declared_wire_model:
        return {"active": True, "allowed": False, "reason": "byte_policy_model_mismatch",
                "declared_wire_model": declared_wire_model,
                "request_wire_model": request_wire_model}
    if not isinstance(count_source, str) or not count_source.strip():
        return {"active": True, "allowed": False, "reason": "count_source_undeclared"}
    if not isinstance(byte_budget, int) or isinstance(byte_budget, bool) or byte_budget <= 0:
        return {"active": True, "allowed": False, "reason": "byte_budget_unavailable"}
    if not isinstance(request_bytes, int) or isinstance(request_bytes, bool) or request_bytes < 0:
        return {"active": True, "allowed": False, "reason": "request_bytes_unavailable"}
    decision = {"active": True, "allowed": request_bytes <= byte_budget,
                "reason": ("within_declared_byte_budget" if request_bytes <= byte_budget
                           else "over_declared_byte_budget"),
                "request_bytes": request_bytes, "byte_budget": byte_budget,
                "count_basis": count_basis, "count_source": count_source,
                "declared_wire_model": declared_wire_model,
                "request_wire_model": request_wire_model,
                "capacity_is_a_guarantee": False,
                "token_count_available": False,
                "operation_guard_not_a_capacity_guarantee": True}
    if not decision["allowed"]:
        decision["over_by"] = request_bytes - byte_budget
    return decision


def capacity_gate_decision(*, agent_tags: Optional[Iterable[str]], prompt_tokens=None,
                           reserve_tokens, context_window: int,
                           count_source: Optional[str] = None,
                           count_basis: Optional[str] = None,
                           request_bytes=None, byte_budget=None,
                           declared_wire_model: Optional[str] = None,
                           request_wire_model: Optional[str] = None) -> dict:
    # The model-specific branch: taken ONLY when the caller declares the byte basis, so the
    # Qwen token path below is byte-for-byte the behaviour it had before.
    if count_basis == BYTE_GATE_COUNT_BASIS:
        return byte_gate_decision(agent_tags=agent_tags, request_bytes=request_bytes,
                                  byte_budget=byte_budget, count_basis=count_basis,
                                  count_source=count_source,
                                  declared_wire_model=declared_wire_model,
                                  request_wire_model=request_wire_model)
    """Decide whether ONE provider request may be sent for one role.

    The gate is active under the declared policy. The input side must be a REAL
    count of THIS request (`prompt_tokens`); there is deliberately no byte-length or
    bytes-per-token fallback, because a ratio can UNDER-estimate (a 10000-byte ASCII
    digit string is 10000 tokens under the official tokenizer) and would then let an
    over-capacity request through. With no count, the decision is a refusal.

    The boundary is the declared `context_window`, and the reserve is THIS role's own
    output budget - the agent's and the auxiliary roles' limits are never mixed.
    """
    policy = policy_state(agent_tags)
    if policy["state"] == "off":
        return {"active": False, "allowed": True, "reason": "no AE capacity policy declared"}
    if policy["state"] == "unknown":
        return {"active": True, "allowed": False, "reason": "unknown_policy_version",
                "tag": policy["tag"]}
    if not isinstance(context_window, int) or context_window <= 0:
        return {"active": True, "allowed": False, "reason": "context_window_unavailable"}
    if not isinstance(reserve_tokens, int) or reserve_tokens < 0:
        return {"active": True, "allowed": False, "reason": "output_reserve_unavailable"}
    if not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        # A MISSING primary count is a refusal. An approximate counter may only
        # tighten a count that already exists (see `capacity_gate_decision` callers),
        # so `count_source` can never turn "no count" into an authorisation.
        return {"active": True, "allowed": False, "reason": "no_trustworthy_count_basis"}
    if not isinstance(count_source, str) or not count_source.strip():
        return {"active": True, "allowed": False, "reason": "count_source_undeclared"}
    decision = {"active": True, "allowed": True, "reason": "within_capacity",
                "input_tokens": prompt_tokens, "reserve_tokens": reserve_tokens,
                "context_window": context_window, "count_source": count_source}
    if prompt_tokens + reserve_tokens > context_window:
        decision.update({"allowed": False, "reason": "input_plus_reserve_exceeds_capacity",
                         "over_by": prompt_tokens + reserve_tokens - context_window})
    return decision


def dict_request_contract(schema_path, model_name: str = "ChatCompletionRequest") -> dict:
    """Read the PINNED request schema's own declaration of the wire request.

    The capacity gate reads `request_data["messages"]` and `request_data["tools"]`
    because `OpenAIClient.build_request_data` ends with
    `request_data = data.model_dump(exclude_unset=True)`. This function makes that
    claim checkable offline: it parses the pinned schema file and returns the declared
    field names of the request model, so a test can require that the gate reads exactly
    the fields the schema declares - without importing the SDK the schema needs.

    `schema_path` is the pinned `letta/schemas/openai/chat_completion_request.py`.
    """
    import ast

    source = Path(schema_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != model_name:
            continue
        fields = []
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                fields.append(statement.target.id)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        fields.append(target.id)
        return {"model": model_name, "declared_fields": sorted(fields),
                "is_pydantic_model": any(
                    isinstance(base, ast.Name) and base.id == "BaseModel"
                    for base in node.bases),
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
    raise NoCompactionPolicyError(f"the pinned schema does not declare {model_name}")
