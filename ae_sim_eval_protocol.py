"""Versioned user-simulator boundary and deterministic evaluation protocol.

This module is a REPAIR LAYER for two measured defects of the r5 R/E pair
(`transfers/ae-deepseek-re-complete-20260916-r5/`). It does not touch the pinned
Vita checkout, the frozen framework, the dataset, any r5 record or any earlier
report. Nothing here imports Vita, opens a socket or calls a model on import.

Two independent, separately versioned protocols:

* ``USER_SIMULATOR_PROTOCOL_*`` -- what the native user simulator is allowed to
  be. The simulator plays the USER only: it may ask, answer, confirm, refuse or
  end the conversation. It must not execute the agent's operations, must not
  invent tool results and must not continue the assistant's turn.
* ``EVALUATION_PROTOCOL_*`` -- how one finished phase is scored. The native
  rubric score, the deterministic business-completion check and any declared
  standard conflict are recorded as three SEPARATE results. A model judgement is
  never re-labelled as a business completion, and a conflict is never counted as
  a capability failure.

Why these exist (measured, not assumed):

1. ``UserSimulator.is_stop`` is `STOP in message.content` (pinned
   `vita/user/user_simulator.py:94-105`) and the R/E driver breaks the exchange
   loop on that boolean. In r5 the simulator repeatedly wrote the AGENT's next
   turn into its own user message -- "user支付成功！... assistant没有了，谢谢。
   ###STOP###" -- so the STOP of a fabricated assistant turn terminated the phase
   before the agent ever reached ``pay_*``. In R t11 the database really was left
   ``unpaid`` while the native reward was 1 (its rubric checks product, spec and
   work address only and carries no payment item), which is a coverage gap, not a
   miscalculation. So: a STOP is accepted only from a clean user turn, and a
   payment claim is only ever credited to a real tool result plus the database.

2. The trajectory judge does not score the dataset rubric verbatim: it echoes
   back an EXPANDED rubric whose added per-merchant distances contradict the
   environment's own coordinates. E t8 ordered from 型动健身空间(银滩路店)
   (103.7145, 36.1055) with home at 103.7195, 36.1005: the pinned haversine tool
   returns 715.0 m (recomputed here: 714.8 m), while the judge's expanded rubric
   lists the same shop as "约2.1km" and excludes it. So: distances are recomputed
   here from the coordinates with the pinned formula, and the disagreement is
   recorded as ``evaluation_conflict`` instead of being silently adjudicated.

3. The judge applied the same purchase fact to the same standard differently
   across arms. Both arms finally bought 全身油压按摩放松券（100分钟）;
   the R judge marked "全身spa券" false, the E judge marked its equivalent true.
   So: identical deterministic facts must produce identical conclusions.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import re

#: The fixed sample user. Used ONLY to rebuild the per-phase key the driver records
#: (`<arm>:<subtask_id>`); it is not a scoring input.
USER_ID = "U000828"

# ---------------------------------------------------------------------------
# Protocol identity
# ---------------------------------------------------------------------------

#: The simulator protocol this module implements. A version string is part of the
#: protocol: the constraint text, the validator rules and the recorded decision
#: shape all belong to it, and a new behaviour means a new version.
USER_SIMULATOR_PROTOCOL = "ae-user-simulator-role-boundary-1"
#: Protocols the runtime accepts. Only the one above is implemented; the native
#: (unconstrained) behaviour is what a run gets when it declares no protocol at
#: all, which is why `None` and not a version string means "unchanged".
USER_SIMULATOR_PROTOCOLS = (USER_SIMULATOR_PROTOCOL,)

#: The evaluation protocol this module implements.
EVALUATION_PROTOCOL = "ae-evaluation-split-1"
EVALUATION_PROTOCOLS = (EVALUATION_PROTOCOL,)

#: The single bundle a repaired run declares. Both arms of a run MUST be handed
#: this same object, which is what makes R and E comparable under one rule set.
PROTOCOL_BUNDLE_FIELDS = ("simulator_protocol", "evaluation_protocol")


class ProtocolDeclarationError(RuntimeError):
    """The run declared a protocol this module does not implement."""


class SimulatorProtocolViolation(RuntimeError):
    """The simulator broke its role boundary. The phase stops; no score is invented.

    This is an INTERACTION-BOUNDARY failure, never a model capability failure, and
    it is never retried: the offending turn, the raw reply and the detected
    evidence are retained in the run record.
    """

    def __init__(self, decision):
        self.decision = decision
        codes = ",".join(item["code"] for item in decision["violations"])
        super().__init__(f"user simulator broke its declared role boundary ({codes})")


def make_protocol_bundle(simulator_protocol=USER_SIMULATOR_PROTOCOL,
                         evaluation_protocol=EVALUATION_PROTOCOL) -> dict:
    """The declared protocol bundle, validated. Raises on anything unreviewed."""
    if simulator_protocol not in USER_SIMULATOR_PROTOCOLS:
        raise ProtocolDeclarationError(
            f"unknown user simulator protocol {simulator_protocol!r}; accepted: "
            f"{list(USER_SIMULATOR_PROTOCOLS)}")
    if evaluation_protocol not in EVALUATION_PROTOCOLS:
        raise ProtocolDeclarationError(
            f"unknown evaluation protocol {evaluation_protocol!r}; accepted: "
            f"{list(EVALUATION_PROTOCOLS)}")
    return {"simulator_protocol": simulator_protocol,
            "evaluation_protocol": evaluation_protocol}


def validate_protocol_bundle(bundle) -> dict:
    """Accept only an explicit, reviewed bundle; never invent a default."""
    if not isinstance(bundle, dict) or set(bundle) != set(PROTOCOL_BUNDLE_FIELDS):
        raise ProtocolDeclarationError(
            "a repaired run must declare exactly the protocol bundle fields "
            f"{list(PROTOCOL_BUNDLE_FIELDS)}")
    return make_protocol_bundle(bundle["simulator_protocol"],
                                bundle["evaluation_protocol"])


def protocol_bundle_sha256(bundle: dict) -> str:
    """Stable digest of a declared bundle, so a record names the exact rules."""
    canonical = json.dumps(validate_protocol_bundle(bundle), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# User-simulator role boundary
# ---------------------------------------------------------------------------

#: The stop markers the PINNED native class treats as terminal
#: (`vita/user/base.py`: STOP, TRANSFER, OUT_OF_SCOPE).
STOP_MARKERS = ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")

#: Violation codes. Every code is a boundary violation of THIS protocol; none of
#: them means "the model could not do the task".
VIOLATION_ASSISTANT_TURN = "simulator_wrote_assistant_turn"
VIOLATION_STOP_CONTINUATION = "simulator_continued_assistant_turn_past_stop_marker"
VIOLATION_QUOTED_AGENT_LINE = "simulator_reproduced_incoming_agent_line"
VIOLATION_LEAKED_STOP_MARKER = "simulator_leaked_stop_marker"
VIOLATION_BAD_FORMAT = "simulator_reply_format_invalid"
VIOLATION_UNKNOWN_ORDER_ID = "simulator_cited_unknown_order_id"
VIOLATION_UNDECLARED_TOOL_EVENT = "simulator_declared_a_tool_event"

#: Business claims the simulator may make but cannot substantiate. These are NOT
#: "continue quietly" violations: they are recorded as unverified business claims
#: and they make the phase fail the strict simulator protocol, so a fabricated
#: "支付成功" can never be counted as a normal completion.
CLAIM_PAYMENT_COMPLETED = "user_claimed_payment_completed_without_database_evidence"

#: The minimum length of an incoming agent line that counts as a leak when the
#: simulator reproduces it verbatim. Chosen above the generic short phrases the
#: r5 transcripts show users really repeating ("谢谢", "没有了"), so a normal
#: quotation of the agent's own short utterance is not a violation.
LEAK_MIN_CHARS = 16

_PAYMENT_DONE = re.compile(
    r"(支付成功|付款成功|已支付|已付款|支付完成|付款完成|支付已成功|支付好了|已经支付|已经付款|付好了|付过了)")
_PAYMENT_ASK = re.compile(r"(是否支付|确认支付|要支付|需要支付|支付吗|付款吗|去支付|要不要支付|支付一下)")
#: A QUESTION about a completion is not a claim of one. This covers the user asking
#: the agent what it meant ("你是说已经支付成功了吗？") as well as the agent asking.
_PAYMENT_QUESTION = re.compile(r"[?？]|吗|呢|么[。！!\s]*$|是不是|难道")
#: A line that only describes the USER's own action or intent, never a result.
_PAYMENT_USER_ACT_ONLY = re.compile(
    r"^(好|好的|行|嗯+|可以|确认|是的|对|ok|OK|OK的)?[，,、。\s]*(我)?(来|去|先)?"
    r"(支付|付款|付|买单|结账)(吧|了|一下|啦|哦|哈)?[。.!！~\s]*$")
#: Order identifiers as the pinned domains really mint them: two uppercase letters
#: plus an alphanumeric body. The body length is bounded below, not above, so a
#: format the minting code changes is REPORTED rather than silently unmatched.
_ORDER_ID = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2}[0-9A-Za-z]{8,})(?![0-9A-Za-z])")
#: Identifiers a user turn can never legitimately quote, because they never appear
#: in an agent message: none. Every cited id is checked against the phase instead.
_LEADING_ROLE = re.compile(r"^[ \t>*\-]*(assistant|user|system|助手|智能体|用户)[ \t]*[:：][ \t]*",
                           re.IGNORECASE | re.MULTILINE)
_LEADING_ROLE_BARE = re.compile(r"^[ \t>*\-]*(assistant|user)[ \t]*$",
                                re.IGNORECASE | re.MULTILINE)
#: A role label that is GLUED to the sentence it introduces ("user支付成功！"). The
#: leading-label rules above need a line break or a colon, which is exactly what the r5
#: instances did not use. This rule is about the LABEL, never about a topic word: the
#: Chinese alternatives are excluded when they are followed by a word that makes the
#: sentence a user's own statement about itself ("用户ID是…", "用户就是我") or when they
#: are a VOCATIVE ("助手，你能帮我确认地址吗？" addresses the assistant and is a normal
#: user turn, while "助手已下单" claims the assistant acted), so a normal user utterance
#: is not mistaken for a role label.
_GLUED_ROLE_LABEL = re.compile(
    r"(?:(?<![0-9A-Za-z])(?:assistant|user)(?![0-9A-Za-z])(?![ \t]*[，,])"
    r"|(?<![\u4e00-\u9fff])(?:助手|智能体)(?![ \t]*[，,、])"
    r"|(?<![\u4e00-\u9fff])用户(?!(?:ID|id|Id|iD|名|号|编号|信息|资料|就是|说|表示)"
    r"(?![ \t]*[，,、])))"
    r"[ \t]*[:：]?[ \t]*(?=[^\s])")
_TOOL_EVENT = re.compile(
    r"(Payment successful|支付接口返回|工具返回(支付|付款)成功|tool\s*(result|返回)\s*[:：]"
    r"|create_(instore_product|delivery)_order\s*\(|pay_(instore|delivery)_order\s*\()",
    re.IGNORECASE)
#: Short conversational filler a USER may put on the same line as the marker. It is
#: stripped before that line's prefix is judged, so "谢谢。 ###STOP###" stays a
#: legitimate ending while a fabricated assistant sentence does not. The label rule is
#: deliberately narrow - the role-label, leaked-line, invented-order-id, payment-claim,
#: multi-line and tool-event rules carry the rest - so a bounded false negative is
#: preferred over flagging a normal user ending.
_USER_FILLER_CHARS = ("没了的谢好嗯对是不用可以就这样辛苦啦吧多谢您事别正来去放松下们我"
                      "有一结束测试离线本次")
# NOTE: the character class below contains NO hyphen. A '-' between two punctuation
# marks is a RANGE in a regex (`!-~` spans every CJK codepoint), which would silently
# accept any text as "filler".
_FILLER_ONLY = re.compile("^[" + _USER_FILLER_CHARS
                          + "，,、。！!？?~；;：:\\s]*$")


def user_simulator_system_suffix(protocol=USER_SIMULATOR_PROTOCOL) -> str:
    """The versioned constraint block appended to the native user system prompt.

    It is appended, never substituted: the pinned prompt keeps its own wording, so
    a run under this protocol differs from an unconstrained run by exactly this
    block. Nothing here mentions a private evaluation answer.
    """
    if protocol not in USER_SIMULATOR_PROTOCOLS:
        raise ProtocolDeclarationError(f"unknown user simulator protocol {protocol!r}")
    return (
        "\n# 用户角色边界（协议 " + protocol + "）\n"
        "- 你只扮演用户。每次回复只写你自己（用户）这一轮要说的话。\n"
        "- 不要替智能体执行任何操作：不要调用工具、不要创建或支付订单、不要预约、不要取消。\n"
        "- 不要编造或转述工具结果、订单状态、支付结果、核销码、骑手或商家动作；"
        "这些只能由智能体通过真实工具返回告诉你。\n"
        "- 不要续写智能体或旁白的台词：你的回复里不要出现 `user`、`assistant` 等角色标签，"
        "也不要出现智能体下一轮才会说的话。\n"
        "- 你提出需求、回答、确认、拒绝或结束对话。确认支付只表示你同意让智能体去支付，"
        "不等于付款已经完成；只有智能体真实返回支付结果后，付款才算完成。\n"
        "- 只有在你确认智能体已经真实完成你的请求、且你不再需要其它帮助时，"
        "才在最后单独一行写 `###STOP###` 结束对话；"
        "不要在别的内容中间夹带该标记，不要替智能体写这个标记。\n"
        "- 如果不想继续，可以直接表示结束；任务是否完成由评测单独判断。\n"
    )


def _content_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


def _split_on_stop(text: str, markers) -> tuple[str, str, str, str, list[str]]:
    """Split a reply at its FIRST stop marker.

    Returns ``(before, after, marker_line_prefix, marker_line_tail, markers)``. The
    marker may sit on its own line or be embedded in one - the r5 instances really
    wrote it as ``"...assistant<text> ###STOP###"`` - so matching only whole lines
    would miss every case this repair exists for. The line prefix/tail around the
    marker are what tell a legitimate bare marker apart from one appended to a
    fabricated assistant turn.
    """
    found = []
    for marker in STOP_MARKERS:
        index = text.find(marker)
        if index >= 0:
            found.append((index, marker))
    if not found:
        return text, "", "", "", []
    at, _marker = min(found)
    present = sorted({marker for index, marker in found
                      if index == at} | {marker for _index, marker in found})
    before, after = text[:at], text[at:]
    for marker in STOP_MARKERS:
        after = after.replace(marker, "")
    prefix = before[before.rfind("\n") + 1:]
    newline = after.find("\n")
    tail = after if newline < 0 else after[:newline]
    return before, after, prefix.strip(), tail.strip(), present


def _quote_norm(text: str) -> str:
    return re.sub(r"[\s*`_#>\-]+", "", text or "")


#: How many verbatim agent lines, in a row, count as the simulator pasting the agent's
#: turn. One quoted line is ordinary user behaviour; two is not a reply any more.
PASTED_RUN_MIN_LINES = 2


#: A line must carry at least this much RAW text to count as a reproduced agent line.
#: The raw length is used, not the punctuation-stripped one, so a rendered bullet such
#: as "- 商品：牛肉塔克(大份)" is still measurable.
PASTED_LINE_MIN_CHARS = 12


def _pasted_agent_run(body: str, incoming: str) -> dict | None:
    """The longest verbatim run of incoming-agent lines inside a user reply.

    Returns ``None`` unless the run reaches ``PASTED_RUN_MIN_LINES``, so quoting one
    line of the agent's message back at it is never a violation and pasting its turn is.
    Comparison ignores punctuation and spacing, but the LENGTH test uses the raw line.
    """
    def substantial(line: str) -> bool:
        return len(line.strip()) >= PASTED_LINE_MIN_CHARS

    wanted = {_quote_norm(line) for line in _content_lines(incoming)
              if substantial(line)}
    if not wanted:
        return None
    run, best = [], None
    for line in _content_lines(body):
        if not line.strip():
            continue          # a blank line is formatting, not a break in the paste
        key = _quote_norm(line)
        if substantial(line) and key in wanted:
            run.append(line.strip())
            if best is None or len(run) > best["lines"]:
                best = {"lines": len(run), "chars": sum(len(x) for x in run),
                        "sha256_source": "\n".join(run)}
        else:
            run = []
    if best is not None and best["lines"] >= PASTED_RUN_MIN_LINES:
        return best
    return None


def _is_payment_claim(text: str) -> bool:
    """A completion claim, not a normal mention of payment.

    Negative controls that must NOT be flagged:
      * "确认支付。" / "好，支付吧。"          -> user agrees, not a result claim
      * "你是说已经支付成功了吗？"            -> asking the agent what it meant
      * "请帮我支付。" / "还要支付吗？"        -> request / question
    """
    for line in _content_lines(text):
        if not line or not _PAYMENT_DONE.search(line):
            continue
        if _PAYMENT_ASK.search(line):
            continue          # a question about payment may contain "支付成功"
        if _PAYMENT_QUESTION.search(line):
            continue          # asking is not asserting
        if _PAYMENT_USER_ACT_ONLY.match(line):
            continue          # the user's own agreement to let the agent pay
        return True
    return False


def validate_user_reply(*, text, incoming_assistant_text="", new_order_ids=(),
                        paid_order_ids=(), visible_orders=None,
                        protocol=USER_SIMULATOR_PROTOCOL) -> dict:
    """Validate ONE native simulator reply against the declared protocol.

    Returns a decision record. Nothing is deleted, rewritten or repaired: the raw
    reply stays in the run record, and the caller stops the phase on a violation.
    """
    if protocol not in USER_SIMULATOR_PROTOCOLS:
        raise ProtocolDeclarationError(f"unknown user simulator protocol {protocol!r}")
    text = text if isinstance(text, str) else ""
    incoming = incoming_assistant_text if isinstance(incoming_assistant_text, str) else ""
    new_order_ids = tuple(x for x in (new_order_ids or ()) if isinstance(x, str))
    paid_order_ids = tuple(x for x in (paid_order_ids or ()) if isinstance(x, str))

    before, after, marker_prefix, marker_tail, markers = _split_on_stop(text, STOP_MARKERS)
    body = (before + "\n" + after).strip()
    nonempty = [line for line in _content_lines(body) if line]
    before_lines = [line for line in _content_lines(before) if line]
    violations: list[dict] = []

    def add(code, detail, evidence):
        violations.append({"code": code, "detail": detail,
                           "evidence": list(evidence or ())})

    # 1. Did the simulator write the assistant's turn?
    #    Evidence required: an EXPLICIT turn label - `assistant:` / `user…` at the start
    #    of a line, or a label glued to the sentence it introduces (`user支付成功！`).
    #    Addressing the assistant ("助手，你能帮我确认地址吗？") is a normal user turn and
    #    is NOT a label; a bare word on its own line is not one either.
    labelled = [m.group(0).strip() for m in _LEADING_ROLE.finditer(body)][:4]
    glued = [m.group(0).strip() for m in _GLUED_ROLE_LABEL.finditer(body)][:4]
    if labelled or glued:
        add(VIOLATION_ASSISTANT_TURN,
            "the user reply carries explicit turn labels, which means the simulator "
            "wrote turns other than its own", (labelled + glued)[:4])

    # 2. Did the simulator continue past a stop marker?
    #    A legitimate ending puts the marker on the user's own final line, possibly
    #    after short filler ("谢谢。 ###STOP###"). It is NOT the user's decision when
    #    the marker is followed by more content, or when the text sharing its line is
    #    real utterance rather than filler - either way the simulator wrote the rest of
    #    a turn and ended the conversation inside it, which is the r5 shape.
    prefix_after_filler = _FILLER_ONLY.sub("", marker_prefix).strip()
    if markers and (after.strip() or len(prefix_after_filler) > 0):
        add(VIOLATION_STOP_CONTINUATION,
            "the terminal marker is not the user's own final line: the simulator "
            "embedded it in text it also wrote, so the end of the conversation was "
            "not the user's own decision",
            [f"marker_line_prefix_chars={len(marker_prefix)}",
             f"marker_line_nonfiller_chars={len(prefix_after_filler)}",
             f"marker_line_tail_chars={len(marker_tail)}",
             f"text_after_marker_chars={len(after.strip())}",
             f"text_before_marker_chars={len(before)}",
             "before_sha256=" + hashlib.sha256(before.encode("utf-8")).hexdigest()])

    # 3. Did the simulator reproduce the incoming agent message in bulk?
    #    Quoting what the agent just said is ordinary user behaviour - "塔克兄弟(地五大道店)
    #    的「牛肉塔克(大份)」34元，就这个吧。" is a NORMAL reply. Only a run of two or more
    #    substantial lines copied verbatim is evidence that the simulator pasted the
    #    agent's turn instead of answering it, and only that is a violation.
    pasted = _pasted_agent_run(body, incoming)
    if pasted:
        add(VIOLATION_QUOTED_AGENT_LINE,
            "the user reply reproduces the incoming agent message line for line; the "
            "simulator is replaying the agent's turn instead of answering it",
            [f"pasted_run_lines={pasted['lines']}",
             f"pasted_run_chars={pasted['chars']}",
             "pasted_run_sha256=" + hashlib.sha256(
                 pasted["sha256_source"].encode("utf-8")).hexdigest()])

    # 4. A stop marker is only legitimate in a message that does not otherwise
    #    claim a result, and only when the simulator really ended the turn.
    if markers and not violations and _is_payment_claim(body):
        add(VIOLATION_LEAKED_STOP_MARKER,
            "the simulator ended the conversation while also asserting a completed "
            "result it cannot know; the terminal marker cannot be trusted",
            [f"markers={list(markers)}"])

    # 5. Tool-level events the simulator cannot have observed (or performed).
    if _TOOL_EVENT.search(body) and not violations:
        add(VIOLATION_UNDECLARED_TOOL_EVENT,
            "the user reply states a tool level event; the simulator neither calls "
            "tools nor receives their results",
            [_TOOL_EVENT.search(body).group(0)])

    # 6. Order identifiers: a user may quote an order the AGENT just showed it, and
    #    the agent may create one in this very phase; anything else is invented.
    # `visible_orders` is the phase's order table AS IT STOOD WHEN THIS TURN WAS
    # ANSWERED; when a caller supplies it, it - not the final table - decides which ids
    # the turn could legitimately know. A later order cannot vouch for an earlier turn.
    if isinstance(visible_orders, dict):
        known = set(visible_orders) | set(_ORDER_ID.findall(incoming))
    else:
        known = set(new_order_ids) | set(_ORDER_ID.findall(incoming))
    cited = set(_ORDER_ID.findall(body))
    unknown = sorted(cited - known)
    if unknown:
        add(VIOLATION_UNKNOWN_ORDER_ID,
            "the user reply cites an order id that this phase neither created nor "
            "received from the agent",
            [f"unknown_order_ids={unknown}", f"known_order_ids={sorted(known)}"])

    # 7. Shape: the only malformed reply is an EMPTY one. A user turn may span several
    #    lines ("请送到公司。\n不要加糖。"); line count is not a role violation, so it is
    #    recorded as an observation and never refused.
    if not text.strip():
        add(VIOLATION_BAD_FORMAT, "the user reply is empty", [])

    # Business claims: recorded separately. They never silently pass.
    claims: list[dict] = []
    if _is_payment_claim(body):
        db_paid = bool(paid_order_ids)
        claims.append({
            "code": CLAIM_PAYMENT_COMPLETED,
            "verified_against_database": db_paid,
            "detail": ("the user asserted a completed payment; the phase's own "
                       "database evidence "
                       + ("shows a paid order, so the claim matches the database"
                          if db_paid else
                          "shows no paid order created in this phase, so the claim is "
                          "unsubstantiated")),
            "evidence": [f"paid_order_ids={sorted(paid_order_ids)}",
                         f"new_order_ids={sorted(new_order_ids)}"],
        })

    original_stop = bool(markers)
    accepted_stop = original_stop and not violations and not claims
    decision = {
        "protocol": protocol,
        "original_stop_marker": original_stop,
        "original_stop_markers": list(markers),
        "accepted_stop": accepted_stop,
        # What the driver acts on: only a clean, substantiated user turn can stop
        # the phase as a normal user_stop.
        "stop": accepted_stop,
        "user_ended_normally": accepted_stop,
        "violations": violations,
        "claims": claims,
        "reply_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "reply_chars": len(text),
        # Recorded for the record, NOT a verdict: a user turn may be several lines.
        "body_lines": len(nonempty),
        "empty": not text.strip(),
    }
    if violations:
        decision["outcome"] = "SIMULATOR_PROTOCOL_VIOLATION"
    elif claims:
        decision["outcome"] = "SIMULATOR_PROTOCOL_UNVERIFIED_CLAIM"
    elif accepted_stop:
        decision["outcome"] = "USER_STOP_ACCEPTED"
    else:
        decision["outcome"] = "USER_TURN_ACCEPTED"
    return decision


# ---------------------------------------------------------------------------
# Deterministic business facts
# ---------------------------------------------------------------------------

#: The pinned Vita distance tool: great-circle distance in metres, rounded to 0
#: (`vita/environment/toolkit.py:271-282`). Recomputed here so a distance fact can
#: be checked without running a model or a tool.
EARTH_RADIUS_M = 6371000.0


def haversine_meters(lon1, lat1, lon2, lat2) -> float:
    """The pinned tool's own formula, step for step."""
    if lon1 == lon2 and lat1 == lat2:
        return 0.0
    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return float(round(EARTH_RADIUS_M * (2 * math.asin(math.sqrt(a))), 0))


def dataset_rubrics(scope: dict) -> dict:
    """`{task_number: [verbatim standard strings]}` from a projected scope.

    `scope["tasks"][i]["rubrics"]` is the standard the JUDGE is actually given
    (the task's own `evaluation_criteria` state_rubrics). The shorter public
    `rubric` list is a different thing: the t4..t12 projection ships 3 public
    rubrics for t11 while the judge scores 4 (it adds the work-address item), so
    anything that means "the standard" must use this list, not the public one.
    """
    out = {}
    for task in (scope or {}).get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("number"), int):
            rubrics = task.get("rubrics")
            out[task["number"]] = [x for x in (rubrics or []) if isinstance(x, str)]
    return out


def scope_task(scope: dict, number: int) -> dict:
    for task in (scope or {}).get("tasks") or []:
        if isinstance(task, dict) and task.get("number") == number:
            return task
    return {}


#: Where a task object may carry the standard the judge is given. `rubrics` is the
#: public projection's field; `evaluation_criteria` is the dataset's own name for the
#: same material; `standard_items` is the keyed form a scoring scope builds. Every
#: source is read verbatim; none is rewritten.
def standard_items(task: dict) -> list[dict]:
    """The task's own standard, as ``[{"rubric_id", "rubric", "source"}, ...]``.

    The ids are the DATASET's own rubric keys: the pinned evaluator mints
    ``rubric_<n>`` by walking ``expected_states[].state_rubrics`` and then
    ``overall_rubrics``, skipping duplicates. They are reproduced here from the
    criteria object itself, so an item is identified by the key the judge was
    actually given - never by a list position this module invented. When a task
    carries only the public projection, the same keys are minted over that list and
    each item says so through ``source``.
    """
    task = task or {}
    explicit = task.get("standard_items")
    if explicit:
        return [{"rubric_id": x["rubric_id"], "rubric": x["rubric"],
                 "source": x.get("source", "dataset_evaluation_criteria")}
                for x in explicit if isinstance(x, dict)
                and isinstance(x.get("rubric_id"), str)
                and isinstance(x.get("rubric"), str)]
    criteria = task.get("evaluation_criteria") or {}
    rows: list[str] = []
    for state in criteria.get("expected_states") or []:
        if isinstance(state, dict):
            rows.extend(x for x in state.get("state_rubrics") or [] if isinstance(x, str))
    rows.extend(x for x in criteria.get("overall_rubrics") or [] if isinstance(x, str))
    source = "dataset_evaluation_criteria"
    if not rows:
        rows = [x for x in task.get("rubrics") or [] if isinstance(x, str)]
        source = "public_rubric_projection"
    out, seen = [], set()
    for text in rows:
        if text in seen:
            continue
        seen.add(text)
        out.append({"rubric_id": f"rubric_{len(out)}", "rubric": text,
                    "source": source})
    return out


def standard_texts(task: dict) -> list[str]:
    """The standard strings this task's own material defines, verbatim."""
    return [x["rubric"] for x in standard_items(task)]


def _order_rows(orders) -> dict:
    return {k: v for k, v in (orders or {}).items() if isinstance(v, dict)}


def new_orders_of(orders, baseline_order_ids) -> dict:
    """The orders THIS phase really created, from the environment database."""
    baseline = set(baseline_order_ids or ())
    return {k: v for k, v in _order_rows(orders).items() if k not in baseline}


def paid_orders_of(orders, baseline_order_ids) -> dict:
    return {k: v for k, v in new_orders_of(orders, baseline_order_ids).items()
            if v.get("status") == "paid"}


def purchase_items(order: dict) -> list[dict]:
    """The items of one order, in one shape, for either order type."""
    return [p for p in (order or {}).get("products") or [] if isinstance(p, dict)]


def item_name(item: dict) -> str:
    return str(item.get("name") or item.get("product_name") or "")


def item_id(item: dict) -> str:
    return str(item.get("product_id") or "")


def attributes_text(item: dict) -> str:
    """An item's own attribute text (规格/糖度/…), from either shape."""
    value = (item or {}).get("attributes")
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(x for x in value if isinstance(x, str))
    return ""


def order_merchant_id(order: dict):
    return (order or {}).get("shop_id") or (order or {}).get("store_id")


def merchant_of(env: dict, order: dict):
    """The merchant record an order points at, from that task's own environment."""
    mid = order_merchant_id(order)
    if not mid:
        return None, None
    for key in ("shops", "stores", "hotels", "attractions"):
        rows = env.get(key)
        if isinstance(rows, dict) and mid in rows:
            return rows[mid], key
    return None, None


def merchant_location(env: dict, merchant) -> dict | None:
    """A merchant's own coordinates out of the task's environment database."""
    if isinstance(merchant, dict):
        loc = merchant.get("location")
        if isinstance(loc, dict) and "longitude" in loc and "latitude" in loc:
            return {"address": loc.get("address"), "longitude": loc["longitude"],
                    "latitude": loc["latitude"]}
    return None


def home_location(task: dict) -> dict | None:
    """The task's own HOME location (what "离我家" refers to).

    Two shapes exist in the fixed projection: some tasks carry one location, others
    carry both the work address and the home address. The home address is resolved
    from the task's own profile (常住住址) and matched against the location rows, so
    the work address is never silently used as "home". Falls back to the single
    recorded row when the profile carries no residential address.
    """
    env = (task or {}).get("environment") or {}
    rows = env.get("location")
    if isinstance(rows, dict):
        rows = [rows]
    rows = [r for r in (rows or []) if isinstance(r, dict)
            and "longitude" in r and "latitude" in r]
    if not rows:
        return None
    profile = (((task or {}).get("user_scenario") or {}).get("user_profile") or {})
    residential = profile.get("常住住址")
    if isinstance(residential, str) and residential:
        head = residential.strip()[:10]
        for row in rows:
            if isinstance(row.get("address"), str) and head and head in row["address"]:
                return {"address": row.get("address"), "longitude": row["longitude"],
                        "latitude": row["latitude"]}
    if len(rows) == 1:
        row = rows[0]
        return {"address": row.get("address"), "longitude": row["longitude"],
                "latitude": row["latitude"]}
    return None


def work_address(task: dict) -> str | None:
    """The work/delivery address a task's own material names, from either shape."""
    task = task or {}
    for candidate in (task.get("user_profile"),
                      (task.get("user_scenario") or {}).get("user_profile")):
        if isinstance(candidate, dict):
            value = candidate.get("工作地址")
            if isinstance(value, str) and value:
                return value
    return None


#: Payment is demanded ONLY when the task's own material demands it. In the t4..t12
#: projection no instruction and no dataset rubric mentions payment (checked
#: verbatim), so this policy keeps the payment requirement OFF for all nine tasks
#: instead of forcing every task through a payment flow. The rule is a predicate,
#: not a memory of this round's orders: a later task whose own instruction truly
#: demands payment turns it on by itself.
PAYMENT_REQUIREMENT_POLICY = "ae-payment-requirement-1"
PAYMENT_REQUIRED_TOKENS = ("支付", "付款", "缴费", "买单", "付钱", "结算")
PAYMENT_NOT_REQUIRED_MARKERS = ("不用支付", "无需支付", "不支付", "别支付", "先不付",
                                "不需要付款", "货到付款")


def task_requires_payment(task: dict, rubric_texts=()) -> dict:
    """Whether THIS task's own material makes payment part of completion."""
    texts = [str(x) for x in (rubric_texts or ())]
    source = "rubric_or_instruction"
    if not texts:
        source = "instruction_only"
    corpus = " ".join(texts + [str((task or {}).get("instruction") or "")])
    if any(marker in corpus for marker in PAYMENT_NOT_REQUIRED_MARKERS):
        return {"required": False, "policy": PAYMENT_REQUIREMENT_POLICY,
                "source": source, "matched": "explicit_not_required",
                "reason": "this task's own material excludes payment"}
    matched = [token for token in PAYMENT_REQUIRED_TOKENS if token in corpus]
    return {"required": bool(matched), "policy": PAYMENT_REQUIREMENT_POLICY,
            "source": source, "matched": matched,
            "reason": ("this task's own rubric/instruction demands payment"
                       if matched else
                       "neither the instruction nor the dataset rubric mentions payment, "
                       "so a payment requirement is NOT imposed on this task")}


#: A standard clause of the form "下单的商品规格应该是X" / "商品的甜度应该是不另外加糖" names
#: a SPEC. The deterministic rule checks it by requiring X to appear in the ordered
#: item's own name or attribute text.
_SPEC_REQUIREMENT = re.compile(
    r"(?:规格|甜度|糖度|份量|分量|容量|尺寸|颜色|口味|时长|次数|类型)"
    r"\s*(?:应该?|必须|需|要)\s*是\s*([^，,。；;、（）()]{1,12})")
#: A QUANTITY rule ("下单数量必须是2杯"). This module has NO quantity check, so such a
#: clause is always reported as uncovered - it must never be absorbed by the opening
#: words of the clause.
_QUANTITY_REQUIREMENT = re.compile(r"(?:数量|件数|个数|杯数|份数|瓶数|张数)[^，,。；;]{0,8}")
#: Sub-clauses this module never decides, whatever else a clause contains (a stated
#: preference rather than a requirement).
_SOFT_STANDARD = re.compile(
    r"优先(?:考虑|推荐|安排|选(?!择))|尽量|最好|如可能|偏好|如果可以")

#: The check families this module can RUN, each mapped to the standard CLAUSE it
#: answers. Coverage is a fact about EXECUTED CHECKS, never about keywords: a clause is
#: credited only when a check of its family really ran for the phase.
CHECK_FAMILIES = ("placement", "cancellation", "payment", "product", "product_positive",
                  "spec", "quantity", "merchant", "distance", "address")
_CLAUSE_FAMILY = (
    ("address", re.compile(r"地址|配送|送达|收货")),
    ("distance", re.compile(r"距离|公里|千米|km|米|附近|最近")),
    ("payment", re.compile(r"支付|付款|缴费|买单|结算|已付")),
    ("cancellation", re.compile(r"取消")),
    ("quantity", re.compile(r"数量|件数|个数|杯数|份数|瓶数|张数")),
    ("spec", re.compile(
        r"规格|甜度|糖度|份量|分量|容量|尺寸|颜色|口味|时长|次数|类型")),
    ("product", re.compile(r"不应选择|不应购买|排除|不得选择")),
    ("product_positive", re.compile(r"商品|产品|套餐|品类")),
    ("merchant", re.compile(r"商家|店铺|门店|店名")),
    # Tested LAST: "下单" opens many clauses that are really about a spec or a count.
    ("placement", re.compile(r"预约|预定|预订|购买|团购|下单(?!数量|份数|杯数|金额|总价)")),
)
#: Families that support a clause only when a VALUE was extracted from it: a clause
#: that merely MENTIONS 规格/距离/商家 with nothing to check covers nothing.
_FAMILY_NEEDS_EXTRACT = ("spec", "distance", "merchant")

#: `约2.3km`, `小于等于2km`, `2 公里` inside a standard clause.
_DISTANCE_IN_STANDARD = re.compile(
    r"(?:约|不超过|小于等于|不少于)?\s*(\d+(?:\.\d+)?)\s*(km|公里|米|m)(?![0-9A-Za-z])",
    re.IGNORECASE)


def rubric_is_soft(text: str) -> bool:
    """Whether a standard clause states a preference this module does not decide."""
    return bool(_SOFT_STANDARD.search(text or ""))


#: Claim separators inside one standard clause. A clause usually asserts SEVERAL things
#: ("下单的商品规格应该是7分糖，不应选择其他糖度规格…"), and each sub-claim needs its own
#: executed check. Coverage is therefore computed per sub-claim: a clause must not be
#: credited for its opening words while a later requirement in it goes unchecked.
_CLAIM_SPLIT = re.compile(r"[，,。；;]|\s+并且\s*|\s*并且\s*|\s*且\s*|\s*同时\s*|\s*以及\s*")

#: The positives in a clause: the part before its first exclusion gloss. "下单的商品应该
#: 是奶茶，不应选择果茶（如…）" is a POSITIVE product clause with a negative gloss;
#: "下单的商品不应该是单部位按摩券" is a pure exclusion.
_POSITIVE_PART = re.compile(r"^(.*?)(?:，不应|,不应|不应选择|不应购买|（如|\(如)")


def _positive_head(text: str) -> str:
    match = _POSITIVE_PART.match(text or "")
    return (match.group(1) if match else (text or ""))


def clause_family(text: str, *, extracted=()) -> str | None:
    """The primary check family a standard clause is about, or None.

    This names the clause kind only; it never asserts that a check ran. `extracted`
    lists the families whose value was really pulled out of the clause, so a clause
    mentioning 规格/距离 without yielding a value covers nothing.
    """
    families = clause_families(text, extracted=extracted)
    return families[0] if families else None


def clause_families(text: str, *, extracted=()) -> list[str]:
    """Every check family a standard clause needs, in the order a check answers them.

    A clause can need more than one: "下单的商品不能是X，也不应选择Y" needs the positive
    product check AND the exclusion check. Coverage requires ONE of them to have run,
    which is the honest reading of "this clause's claim was answered".
    """
    text = text or ""
    if _SOFT_STANDARD.search(text):
        return []
    extracted = set(extracted or ())
    head = _positive_head(text)
    out: list[str] = []
    for family, pattern in _CLAUSE_FAMILY:
        target = head if family == "product_positive" else text
        if not pattern.search(target):
            continue
        if family in _FAMILY_NEEDS_EXTRACT and family not in extracted:
            continue
        out.append(family)
    return out


def claim_segments(text: str) -> list[str]:
    """A standard clause split into its sub-claims, blanks and connectors dropped."""
    out = []
    for raw in _CLAIM_SPLIT.split(text or ""):
        piece = raw.strip().strip("，,。；;、（）() ")
        if piece:
            out.append(piece)
    return out


def uncovered_claims(text: str, executed_checks=()) -> list[str]:
    """The sub-claims of a clause that no executed check answers.

    Coverage is per SUB-CLAIM, never for the clause as a whole: "下单数量必须是2杯"
    must not be credited to the placement check just because it opens with 下单.
    """
    families = {family for _requirement, family in executed_checks}
    missing = []
    for segment in claim_segments(text):
        if _SOFT_STANDARD.search(segment):
            continue
        extracted = set()
        if spec_requirements([segment]):
            extracted.add("spec")
        if distance_limit_m([segment]) is not None:
            extracted.add("distance")
        if _EXCLUSION_BLOCK.search(segment):
            extracted.add("merchant")
        if _QUANTITY_REQUIREMENT.search(segment):
            needed_quantity = ["quantity"]
            if "quantity" not in families:
                missing.append(segment)
            continue
        needed = clause_families(segment, extracted=extracted)
        if needed and not any(family in families for family in needed):
            missing.append(segment)
    return missing


def standard_item_is_covered(text: str, *, executed_checks=()) -> bool:
    """Whether every sub-claim of a standard clause is answered by a check that RAN.

    `executed_checks` is the set of `(requirement, family)` pairs the deterministic
    result really produced, so an unimplemented requirement (a quantity rule, say) is
    reported as uncovered instead of being credited to a word in the clause.
    """
    segments = claim_segments(text or "")
    if not segments:
        return False
    missing = uncovered_claims(text, executed_checks=executed_checks)
    if missing:
        return False
    # A clause nothing could classify is NOT covered: silence is never a pass.
    return any(clause_families(segment) for segment in segments)


def spec_requirements(standard_texts_pool) -> list[dict]:
    """The spec values a standard names, as `[{"value", "clause"}, ...]`."""
    out = []
    for text in standard_texts_pool or ():
        for value in _SPEC_REQUIREMENT.findall(text or ""):
            value = value.strip()
            if value:
                out.append({"value": value, "clause": (text or "")[:120]})
    return out


def item_satisfies_spec(item: dict, value: str) -> bool:
    """Whether an ordered item's own name/attribute text states the required spec."""
    haystack = f"{item_name(item)} {attributes_text(item)}"
    return bool(value) and value in haystack


def distance_limit_m(standard_texts) -> float | None:
    """The distance bound a standard states, in metres, or None when it states none."""
    for text in standard_texts or ():
        for value, unit in _DISTANCE_IN_STANDARD.findall(text or ""):
            number = float(value)
            if unit.lower() in ("km", "公里"):
                return number * 1000.0
            return number
    return None


#: A standard that forbids specific products states them as examples:
#: `不应选择其他IP联名款（如茶百道×未定事件簿联名·律动莓莓奶茶、…）`. The names inside
#: such a clause are read out and checked against what was actually bought, which is the
#: same "except the named ones" content the clause states - no product is inferred.
_EXCLUSION_BLOCK = re.compile(r"不应选择[^。]*?[（(]如([^）)]*)[）)]")
_EXCLUDED_NAME = re.compile(r"[^、，,；;（）()\s]{2,40}")


def excluded_product_names(standard_texts_pool) -> list[str]:
    """Product names a standard explicitly lists as examples of what NOT to buy."""
    out: list[str] = []
    for text in standard_texts_pool or ():
        for block in _EXCLUSION_BLOCK.findall(text or ""):
            for name in _EXCLUDED_NAME.findall(block):
                if name and name not in out:
                    out.append(name)
    return out


def excluded_merchants_in_standard(standard_text: str, env: dict) -> list[str]:
    """Merchant ids a standard explicitly names as NOT to be chosen.

    The name is resolved against THIS task's own environment, so the exclusion is tied
    to the data the phase really had rather than to a copied id.
    """
    text = standard_text or ""
    if "不应选择" not in text:
        return []
    markets = {}
    for key in ("shops", "stores"):
        rows = env.get(key)
        if isinstance(rows, dict):
            markets.update(rows)
    out = []
    for merchant_id, row in markets.items():
        name = (row or {}).get("shop_name") or (row or {}).get("store_name")
        if isinstance(name, str) and name and name in text:
            out.append(merchant_id)
    return sorted(out)


#: The status the pinned order model calls cancelled (`vita/data_model/tasks.py`
#: OrderStatus). This is a CANCELLATION fact, not a payment one.
ORDER_STATUS_CANCELLED = ("cancelled",)
#: The statuses that mean the purchase is paid for. This is a PAYMENT fact and is
#: checked ONLY when the task's own standard demands payment; folding it into a
#: "not cancelled" check would silently impose a payment requirement on every task.
ORDER_STATUS_PAID = ("paid", "unconsumed", "consumed", "delivered")


def order_address(order: dict) -> str | None:
    """The delivery/consumption address of an order, from whatever shape it has.

    The live orders carry a nested location object; a projected or older record may
    carry a plain string. Comparing `str(dict)` with an address silently PASSES every
    order, so the address is read structurally and an unreadable shape yields None.
    """
    location = (order or {}).get("location")
    if isinstance(location, str):
        return location or None
    if isinstance(location, dict):
        address = location.get("address")
        return address if isinstance(address, str) and address else None
    return None


def order_meets_purchase(order: dict, *, product_ids=(), merchant_id=None,
                         work_address_text=None, excluded_merchant_ids=(),
                         distance_limit_m=None, distance_from_home_m=None,
                         spec_values=(), excluded_product_names_pool=()) -> dict:
    """The deterministic purchase facts of ONE real order.

    Every check reports its own `covered` state: a check whose rule is ABSENT from the
    standard is `covered: False` and is never counted as met, so a missing rule can
    never be mistaken for a satisfied one.
    """
    items = purchase_items(order)
    ids = {item_id(x) for x in items if item_id(x)}
    target_ids = {str(x) for x in (product_ids or ()) if x}
    excluded = {str(x) for x in (excluded_merchant_ids or ()) if x}
    status = order.get("status")
    address = order_address(order)

    def check(met, *, value, rule, covered=True):
        return {"met": bool(met), "value": value, "rule": rule, "covered": covered}

    checks = {
        "order_created": check(True, value=order.get("order_id"),
                               rule="an order created in THIS phase exists"),
        "order_not_cancelled": check(
            status not in ORDER_STATUS_CANCELLED, value=status,
            rule={"not_in": list(ORDER_STATUS_CANCELLED)}),
        "order_paid": check(
            status in ORDER_STATUS_PAID, value=status, rule=list(ORDER_STATUS_PAID)),
        "target_product": check(
            (not target_ids) or bool(ids & target_ids), value=sorted(ids),
            rule=sorted(target_ids), covered=bool(target_ids)),
        "spec_values": check(
            all(item_satisfies_spec(x, v) for v in spec_values for x in items)
            if (spec_values and items) else not spec_values,
            value=spec_values, rule=spec_values, covered=bool(spec_values)),
        "items_not_excluded_by_name": check(
            (not excluded_product_names_pool)
            or not any(name and (name in item_name(x) or name == item_id(x))
                       for name in excluded_product_names_pool for x in items),
            value=[{"name": item_name(x), "product_id": item_id(x)} for x in items],
            rule=excluded_product_names_pool,
            covered=bool(excluded_product_names_pool)),
        "merchant_not_excluded": check(
            (not excluded) or (order_merchant_id(order) not in excluded),
            value=order_merchant_id(order), rule=sorted(excluded), covered=bool(excluded)),
        "merchant": check(
            (merchant_id is None) or (order_merchant_id(order) == merchant_id),
            value=order_merchant_id(order), rule=merchant_id,
            covered=merchant_id is not None),
        "work_address": check(
            (work_address_text is None) or (address == work_address_text),
            value=address, rule=work_address_text, covered=work_address_text is not None),
    }
    if distance_limit_m is not None:
        checks["merchant_distance"] = check(
            distance_from_home_m is not None and distance_from_home_m <= distance_limit_m,
            value=distance_from_home_m, rule={"max_m": distance_limit_m},
            covered=distance_from_home_m is not None)
    return {"order_id": order.get("order_id"), "status": status,
            "total_price": order.get("total_price"),
            "address": address,
            "items": [{"product_id": item_id(x), "name": item_name(x),
                       "attributes": attributes_text(x)} for x in items],
            "checks": checks,
            "all_met": all(v["met"] for v in checks.values()),
            "uncovered_checks": sorted(k for k, v in checks.items() if not v["covered"]),
            "failed_checks": sorted(k for k, v in checks.items()
                                    if v["covered"] and not v["met"])}


# ---------------------------------------------------------------------------
# Declared conflicts: standards that cannot be adjudicated from the evidence
# ---------------------------------------------------------------------------

CONFLICT_RUBRIC_TEXT_DIVERGES = "rubric_text_diverges_from_dataset_standard"
CONFLICT_TOOL_FACT_VS_STANDARD = "standard_contradicts_environment_and_tool_fact"
CONFLICT_INDETERMINATE = "indeterminate_from_evidence"

#: Declared, TASK-LEVEL conflicts for the fixed t4..t12 projection. Each one is
#: evidence about the measurement, not about the model. They are deliberately keyed
#: by task number, never by an order id, and they never choose a side: a task listed
#: here is reported as unfit for a clean R/E comparison on the named item.
DECLARED_STANDARD_CONFLICTS = (
    {
        "task_number": 5,
        "kind": CONFLICT_RUBRIC_TEXT_DIVERGES,
        "item": "下单的商品应该是全身spa券 / 优先选择时长较长…带精油等放松元素的全身spa产品",
        "status": CONFLICT_INDETERMINATE,
        "summary": ("Both arms ended on the SAME purchase (P00081 全身油压按摩放松券（100分钟）, "
                    "248元, tags 全身/油压按摩/放松), while the task's own dataset standard "
                    "names two different things at once: criterion 0 excludes 全身油压按摩券 "
                    "as non-spa, and criterion 3 *prefers* a 120-minute oil-and-spa product, "
                    "whose dataset target is P00067 全身spa精油按摩券（120分钟）. The two arms' "
                    "model judges then split on the SAME fact: R judged the 100-minute purchase "
                    "insufficient for criterion 3, E judged it '略短于120分钟但仍属较长时长…基本符合'. "
                    "A stated preference ('优先') being scored as a hard requirement is the "
                    "divergence; neither arm's answer can be called wrong from the evidence."),
        "evidence": [
            "dataset .ae-verify-src/tasks-full-a4553e1.json sub_U000828_5 "
            "evaluation_criteria.expected_states[0].state_rubrics[0,3]",
            "same task target_product_ids = ['S17791041709546489_P00067'] "
            "(全身spa精油按摩券（120分钟）)",
            "task-evidence.json: rewrites t5 and erratum t5 nl_rubrics item 3, and both "
            "tasks' new order products (P00081, 248.0, paid / unpaid)",
        ],
        "unresolved": ["is '优先选择' a pass/fail criterion or a tie-breaker?",
                       "is 全身油压按摩 a 全身spa券 for this standard?"],
    },
    {
        "task_number": 8,
        "kind": CONFLICT_TOOL_FACT_VS_STANDARD,
        "item": "下单的商家距离…飞天世纪新城应该小于等于2km",
        "status": CONFLICT_INDETERMINATE,
        "summary": ("The task's OWN dataset standard carries per-merchant distance "
                    "annotations: 型动健身空间(银滩路店) is called '约2.1km' and excluded. "
                    "That merchant's own coordinates (103.7145, 36.1055) against the same "
                    "task's home coordinates (103.7195, 36.1005) give 715.0m by the pinned "
                    "haversine formula - exactly what the pinned distance tool returned in "
                    "the recorded call. All seven annotated merchants are 2.6x-3.6x farther "
                    "in the annotations than their own coordinates imply (647m~'2.3km' .. "
                    "1189m~'3.2km'), so the annotations are not straight-line distances and "
                    "no single home point reproduces them (best fit RMSE 504m). The model "
                    "judge only echoed this standard; it did not invent it."),
        "evidence": [
            "dataset .ae-verify-src/tasks-full-a4553e1.json sub_U000828_8 "
            "evaluation_criteria.expected_states[0].state_rubrics[1] (the annotated text)",
            "same task environment.location[0] + environment.shops"
            "[S17791041973348453_I00005].location (the coordinates)",
            "task-evidence.json: erratum t8 transcript[7] tool call, [11] result 715.0",
            "recomputed here with the pinned formula: ae_sim_eval_protocol.haversine_meters "
            "-> 715.0",
        ],
        "unresolved": ["which distance the annotations were computed from",
                       "whether the 2km limit is straight-line or travel distance",
                       "whether the annotated merchants were meant to be excluded at all"],
    },
)


# ---------------------------------------------------------------------------
# The three separated results
# ---------------------------------------------------------------------------

def native_rubric_result(judge: dict, dataset_rubric_texts=(), standard_item_pool=()) -> dict:
    """Result 1: the native rubric score, kept in its own terms.

    The native reward is recorded verbatim, together with a stable per-item identity
    that aligns each item to the STANDARD by position. The item text the model
    echoed back is retained as the judge's own rendering, never as the standard.
    """
    info = (judge or {}).get("reward_info") or {}
    nl = info.get("nl_rubrics")
    pool = standard_items({"standard_items": standard_item_pool}) \
        if standard_item_pool else standard_items(
            {"rubrics": list(dataset_rubric_texts or ())})
    items = []
    for index, entry in enumerate(nl if isinstance(nl, list) else []):
        if not isinstance(entry, dict):
            continue
        declared_id = entry.get("rubric_idx")
        # The item's identity is the DATASET's rubric key the judge was given. It is
        # matched by that key; a positional fallback is used only when the judge named
        # no key, and it is recorded as such rather than passed off as a verified id.
        by_key = next((x for x in pool if x["rubric_id"] == declared_id), None)
        fallback = pool[index] if index < len(pool) else None
        matched = by_key or fallback
        standard = matched["rubric"] if matched else None
        echoed = entry.get("nl_rubric")
        items.append({
            "rubric_id": matched["rubric_id"] if matched else None,
            "rubric_id_source": ("dataset_key" if by_key is not None
                                 else ("position_fallback" if fallback is not None
                                       else "unmatched")),
            "rubric_id_declared": declared_id,
            "rubric_id_matches_declared": (by_key is not None
                                           and declared_id is not None),
            "standard_source": matched["source"] if matched else None,
            "met": entry.get("met") is True,
            "dataset_rubric": standard,
            "judge_rubric_text": echoed,
            "judge_rubric_matches_dataset": (standard is not None and echoed == standard),
            "justification": entry.get("justification"),
        })
    return {
        "reward": info.get("reward"),
        "rubrics_met": sum(1 for x in items if x["met"]),
        "rubrics_total": len(items),
        "items": items,
        "judge_status": (judge or {}).get("status"),
        "note": ("the native rubric keeps its original meaning; if it carries no "
                 "payment item it is NOT a payment acceptance test"),
    }


def deterministic_business_result(*, arm, number, task, scope_task_record, orders,
                                  baseline_order_ids, dataset_rubric_texts=(),
                                  transcript=(), standard_item_pool=(),
                                  excluded_merchant_ids=()) -> dict:
    """Result 2: completion from the phase's own real orders, never from text.

    Reads only the environment database captured for THIS phase: an order is new
    because its id is absent from the ids captured before the phase started, so a
    historical or earlier-phase order can never stand in for this phase's work.
    """
    task_number = int(number)
    requirement = task_requires_payment(task, dataset_rubric_texts)
    baseline = sorted(baseline_order_ids or ())
    created = new_orders_of(orders, baseline_order_ids)
    paid = paid_orders_of(orders, baseline_order_ids)
    target_ids = [x for x in (scope_task_record or {}).get("target_product_ids") or []
                  if isinstance(x, str)]
    work = work_address(task)
    env = (task or {}).get("environment") or {}
    home = home_location(task)
    # The STANDARD the checks are anchored to, as keyed items: either the explicit pool
    # the caller supplies (the scoring scope) or the task's own criteria.
    standard_item_pool = list(standard_item_pool or standard_items(
        {"rubrics": list(dataset_rubric_texts or ())}))
    standard_texts_pool = [x["rubric"] for x in standard_item_pool]
    spec_values = [x["value"] for x in spec_requirements(standard_texts_pool)]
    excluded_names = excluded_product_names(standard_texts_pool)
    excluded_merchant_ids = set(excluded_merchant_ids or ()) | set(
        excluded_merchants_in_standard("\n".join(standard_texts_pool), env))

    rows = []
    for order_id in sorted(created):
        order = created[order_id]
        merchant, collection = merchant_of(env, order)
        location = merchant_location(env, merchant)
        distance = None
        if location and home:
            distance = haversine_meters(home["longitude"], home["latitude"],
                                        location["longitude"], location["latitude"])
        row = order_meets_purchase(
            order, product_ids=target_ids, work_address_text=work,
            excluded_merchant_ids=excluded_merchant_ids,
            distance_limit_m=distance_limit_m(standard_texts_pool),
            distance_from_home_m=distance,
            spec_values=spec_values,
            excluded_product_names_pool=excluded_names)
        row["merchant"] = {
            "id": order_merchant_id(order),
            "name": (merchant or {}).get("shop_name") or (merchant or {}).get("store_name"),
            "collection": collection,
            "location": location,
        }
        row["distance_from_home_m"] = distance
        row["distance_formula"] = "pinned haversine, rounded to 0 m"
        row["paid"] = order.get("status") == "paid"
        rows.append(row)

    # Which checks were GENERATED and RUN for this phase, each with the standard-clause
    # family it answers. Coverage is derived from THIS list only: a requirement that was
    # never generated (a quantity rule, say) cannot be credited to a keyword.
    executed: list[tuple[str, str]] = []

    def _check(requirement_name, family, met, *, rule, value, covered=True,
               source=None, note=None):
        out = {"met": bool(met), "covered": bool(covered), "rule": rule, "value": value,
               "family": family}
        if covered:
            executed.append((requirement_name, family))
        if source is not None:
            out["source"] = source
        if note is not None:
            out["note"] = note
        return out

    def all_or_none(requirement_name, family, key, *, rule, value, covered=True):
        """`met` over every order: False when there are no orders to judge."""
        if not rows:
            return _check(requirement_name, family, False, rule=rule, value=value,
                          covered=covered)
        return _check(requirement_name, family,
                      all(r["checks"][key]["met"] for r in rows), rule=rule,
                      value=value, covered=covered)

    requirements = {
        "order_created_in_this_phase": _check(
            "order_created_in_this_phase", "placement", bool(rows),
            rule="a new order id absent from the pre-phase baseline",
            value=sorted(created)),
        "order_not_cancelled": all_or_none(
            "order_not_cancelled", "cancellation", "order_not_cancelled",
            rule="the phase's order must not be in a cancelled status",
            value={k: v.get("status") for k, v in sorted(created.items())}),
        "items_match_target_product": all_or_none(
            "items_match_target_product", "product_positive", "target_product",
            rule=target_ids or None, value=[r["items"] for r in rows],
            covered=bool(target_ids)),
    }
    # The work-address rule is a DELIVERY rule. The pinned order model carries a
    # delivery `location` only for delivery orders (`vita/data_model/tasks.py`
    # `Order.location`), so an instore phase is recorded as NOT APPLICABLE instead of
    # being failed for missing an address it was never supposed to have.
    delivered = [r for r in rows
                 if created.get(r["order_id"], {}).get("order_type") == "delivery"]
    if delivered:
        requirements["delivery_at_work_address"] = _check(
            "delivery_at_work_address", "address",
            all(r["checks"]["work_address"]["met"] for r in delivered),
            rule=work, value=[r["checks"]["work_address"]["value"] for r in delivered],
            covered=work is not None)
    else:
        requirements["delivery_at_work_address"] = _check(
            "delivery_at_work_address", "address", False, rule=work, value=[],
            covered=False,
            note=("no delivery order in this phase; the delivery-address rule does not "
                  "apply and is not counted as met"))
    if excluded_names:
        requirements["items_not_excluded_by_name"] = all_or_none(
            "items_not_excluded_by_name", "product", "items_not_excluded_by_name",
            rule=excluded_names,
            value=[[{"name": i["name"], "product_id": i["product_id"]}
                    for i in r["items"]] for r in rows])
    excluded = excluded_merchant_ids
    if excluded:
        requirements["merchant_not_excluded"] = all_or_none(
            "merchant_not_excluded", "merchant", "merchant_not_excluded",
            rule=sorted(excluded),
            value=[order_merchant_id(created[r["order_id"]]) for r in rows])
    specs = spec_requirements(standard_texts_pool)
    if specs:
        requirements["items_match_spec_values"] = all_or_none(
            "items_match_spec_values", "spec", "spec_values",
            rule=specs, value=[[i["name"] + " " + i["attributes"] for i in r["items"]]
                               for r in rows], covered=True)
    distance_rule = distance_limit_m(standard_texts_pool)
    if distance_rule is not None:
        requirements["merchant_distance"] = all_or_none(
            "merchant_distance", "distance", "merchant_distance",
            rule={"max_m": distance_rule},
            value=[r["distance_from_home_m"] for r in rows],
            covered=all(r["distance_from_home_m"] is not None for r in rows) if rows else False)
    if requirement["required"]:
        # PAYMENT is its own check, generated only when the task's own standard demands
        # it. It is never folded into the cancellation check.
        requirements["payment_completed"] = all_or_none(
            "payment_completed", "payment", "order_paid",
            rule="the phase's own database must show the new order paid",
            value={k: v.get("status") for k, v in sorted(created.items())})
        requirements["payment_completed"]["source"] = requirement

    unmet = sorted(k for k, v in requirements.items() if v["covered"] and not v["met"])
    uncovered = sorted(k for k, v in requirements.items() if not v["covered"])
    # Uncovered standard clauses: every clause whose family no EXECUTED check answers.
    # These are LISTED, never silently treated as satisfied, and they block `complete`.
    uncovered_items = [
        {"rubric_id": x["rubric_id"], "rubric": x["rubric"], "source": x["source"],
         "uncovered_claims": uncovered_claims(x["rubric"], executed_checks=executed),
         "why": ("no check of this sub-claim's family was generated for this phase; it "
                 "is recorded, not judged")}
        for x in standard_items({"standard_items": standard_item_pool})
        if not standard_item_is_covered(x["rubric"], executed_checks=executed)]
    recorded_attributes = sorted({item["attributes"] for r in rows for item in r["items"]
                                  if item["attributes"]})
    complete = bool(rows) and not unmet and not uncovered and not uncovered_items
    # WHY an incomplete phase is incomplete. `.complete=False` alone must never be read
    # as "the task failed": the three cases are different facts.
    if complete:
        gap_kind = None
    elif unmet:
        gap_kind = "a_check_failed"
    elif uncovered or uncovered_items:
        gap_kind = "the_standard_is_not_fully_covered_by_this_evaluator"
    else:
        gap_kind = "no_order_was_created_in_this_phase"
    return {
        "protocol": EVALUATION_PROTOCOL,
        "arm": arm, "task_number": task_number,
        "baseline_order_ids": baseline,
        "new_order_ids": sorted(created),
        "paid_order_ids": sorted(paid),
        "statuses": {k: v.get("status") for k, v in sorted(created.items())},
        "payment_requirement": requirement,
        "requirements": requirements,
        "purchase_facts": rows,
        "checked_requirements": sorted(k for k, v in requirements.items() if v["covered"]),
        "executed_checks": sorted(f"{requirement}:{family}"
                                  for requirement, family in executed),
        "uncovered_requirements": uncovered,
        "uncovered_standard_items": uncovered_items,
        "recorded_item_attributes": recorded_attributes,
        # `complete` is a claim about the CHECKS, not about the task: it is false unless
        # every requirement is covered AND met AND the standard has nothing left for a
        # human/judge to decide. A narrow evaluator must not report overall success.
        "complete": complete,
        "completion_gap_kind": gap_kind,
        "completeness_basis": "all covered requirements met AND no uncovered standard item",
        "unmet_requirements": unmet,
        "basis": "environment database captured for this phase; no text is used",
        "scope_note": ("deterministic sub-item checks only; it does not re-implement the "
                       "whole scoring standard"),
    }


def evaluation_conflict_result(*, arm, number, business, native, task,
                               dataset_rubric_texts=(), public_rubric_texts=None) -> dict:
    """Result 3: does a standard conflict exist, or is the verdict undecidable?

    Independent sources are compared and NEITHER is chosen: a declared task conflict
    names the task, and the judge's echoed item text is checked against the standard
    it was given. An item whose echoed text differs from that standard is recorded
    as undecidable rather than attributed to the standard as written.
    """
    conflicts: list[dict] = []
    task_number = int(number)
    for declared in DECLARED_STANDARD_CONFLICTS:
        if declared["task_number"] == task_number:
            conflicts.append(deepcopy(declared))

    # A judge item is a conflict ONLY when it rejects what the environment can
    # verify, so this is checked against this phase's own deterministic facts.
    deterministic_ok = bool(business.get("complete"))
    judge_failed = [x["rubric_id"] for x in native.get("items") or [] if not x["met"]]
    if judge_failed and deterministic_ok and business.get("new_order_ids"):
        conflicts.append({
            "task_number": task_number, "kind": CONFLICT_RUBRIC_TEXT_DIVERGES,
            "item": "judge rubric items",
            "status": CONFLICT_INDETERMINATE,
            "summary": ("the model judge rejected item(s) "
                        + ",".join(judge_failed)
                        + " while the phase's own deterministic facts are satisfied; "
                          "which requirement was applied cannot be settled from the "
                          "standard as written"),
            "evidence": ["deterministic_business_result.purchase_facts"],
            "unresolved": ["which additional requirement the judge applied"],
        })

    unknown = []
    for item in native.get("items") or []:
        if item.get("dataset_rubric") is not None and not item["judge_rubric_matches_dataset"]:
            unknown.append({"rubric_id": item["rubric_id"],
                            "why": "the judge echoed a rubric text that differs from the "
                                   "standard it was given; the item cannot be attributed "
                                   "to that standard as written"})

    # A published standard that is a strict SUBSET of the scored standard means the
    # run's own public material under-states what is scored. This is recorded, never
    # resolved here: t11's public list has 3 items while the judge scores 4.
    divergence = None
    if public_rubric_texts is not None:
        public_count, scored_count = len(list(public_rubric_texts)), len(dataset_rubric_texts or ())
        if scored_count and public_count and scored_count != public_count:
            divergence = {
                "kind": "scored_standard_larger_than_public_rubric",
                "public_rubric_items": public_count, "scored_items": scored_count,
                "summary": ("the scored standard carries more items than the run's public "
                            "rubric list; items beyond the public list are scored but "
                            "never published with the task"),
            }
    return {
        "protocol": EVALUATION_PROTOCOL,
        "arm": arm, "task_number": task_number,
        "has_conflict": bool(conflicts),
        "conflicts": conflicts,
        "public_standard_divergence": divergence,
        "undecidable_items": unknown,
        "is_capability_failure": False,
        "note": ("a declared conflict or an undecidable item is NOT a model capability "
                 "failure and is NOT an automatic success"),
    }


def evaluate_phase_record(*, arm, number, task, task_record, scope_task_record,
                          public_task_record=None) -> dict:
    """The full three-part result for ONE finished phase record (r5 shape)."""
    judge = (task_record or {}).get("judge") or {}
    pool = standard_items(scope_task_record)
    standard = [x["rubric"] for x in pool]
    public = ([x for x in (public_task_record or {}).get("rubrics") or []
               if isinstance(x, str)] if public_task_record is not None else None)
    snapshot = (task_record or {}).get("native_snapshot") or {}
    env = snapshot.get("environment_db") or {}
    orders = env.get("orders") or {}
    baseline = (task_record or {}).get("baseline_order_ids") or []
    native = native_rubric_result(judge, standard, standard_item_pool=pool)
    business = deterministic_business_result(
        arm=arm, number=number, task=task, scope_task_record=scope_task_record,
        orders=orders, baseline_order_ids=baseline,
        dataset_rubric_texts=standard, standard_item_pool=pool,
        transcript=(task_record or {}).get("transcript") or ())
    conflict = evaluation_conflict_result(
        arm=arm, number=number, business=business, native=native, task=task,
        dataset_rubric_texts=standard, public_rubric_texts=public)
    exclusion = ([f"declared_conflict:{x['kind']}:{x['item']}" for x in conflict["conflicts"]]
                 + [f"undecidable:{x['rubric_id']}" for x in conflict["undecidable_items"]])
    return {
        "protocol": EVALUATION_PROTOCOL,
        "bundle_sha256": protocol_bundle_sha256(make_protocol_bundle()),
        "arm": arm, "task_number": int(number),
        "subtask_id": (task_record or {}).get("subtask_id"),
        "standard_items_scored": len(standard),
        "native_rubric_score": native,
        "business_completion": business,
        "evaluation_conflict": conflict,
        "comparison_eligible": not exclusion,
        "comparison_exclusion_reasons": exclusion,
    }


# ---------------------------------------------------------------------------
# Reports (added NEXT TO the old report, never instead of it)
# ---------------------------------------------------------------------------

def build_evaluation_report(result: dict, *, scope: dict | None = None,
                            public_scope: dict | None = None,
                            simulator_decisions: dict | None = None,
                            simulator_decisions_source: str = "not_supplied",
                            bundle: dict | None = None) -> dict:
    """The separated report for one R/E run record.

    The old report is untouched: this object carries the native reward in its
    original terms alongside the deterministic and conflict results, and it states
    which tasks are unfit for a clean R/E comparison.
    """
    bundle = validate_protocol_bundle(bundle or make_protocol_bundle())
    scope = scope or result.get("scope") or {}
    tasks_by_number = {}
    for task in scope.get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("number"), int):
            tasks_by_number[task["number"]] = task
    public_by_number = {}
    for task in (public_scope or {}).get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("number"), int):
            public_by_number[task["number"]] = task
    decisions = simulator_decisions or {}
    phases = []
    for arm in (result.get("arms") or {}):
        for record in (result.get("arms") or {}).get(arm, {}).get("tasks") or []:
            number = record.get("number")
            phase = evaluate_phase_record(
                arm=arm, number=number, task=tasks_by_number.get(number) or {},
                task_record=record, scope_task_record=tasks_by_number.get(number) or {},
                public_task_record=public_by_number.get(number))
            key = f"{arm}:{record.get('subtask_id')}"
            phase["user_simulator"] = deepcopy(decisions.get(key)) or {
                "protocol": bundle["simulator_protocol"],
                "recorded": False,
                "outcome": "NOT_RECORDED",
                "note": ("no simulator decision was captured for this phase; the "
                         "simulator protocol is therefore unverified for it"),
            }
            phases.append(phase)
    totals = {}
    for arm in (result.get("arms") or {}):
        rows = [p for p in phases if p["arm"] == arm]
        totals[arm] = {
            "phases": len(rows),
            "native_reward_sum": sum((p["native_rubric_score"]["reward"] or 0)
                                     for p in rows),
            "native_rubrics_met": sum(p["native_rubric_score"]["rubrics_met"] for p in rows),
            "native_rubrics_total": sum(p["native_rubric_score"]["rubrics_total"] for p in rows),
            "business_complete": sum(1 for p in rows if p["business_completion"]["complete"]),
            "evaluation_conflicts": sum(1 for p in rows if p["evaluation_conflict"]["has_conflict"]),
            "comparison_eligible": sum(1 for p in rows if p["comparison_eligible"]),
            "simulator_protocol_failures": sum(
                1 for p in rows
                if p["user_simulator"].get("outcome") == "SIMULATOR_PROTOCOL_VIOLATION"),
        }
    return {
        "protocol": EVALUATION_PROTOCOL,
        "bundle": bundle,
        "bundle_sha256": protocol_bundle_sha256(bundle),
        "simulator_protocol": bundle["simulator_protocol"],
        "evaluation_protocol": bundle["evaluation_protocol"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_schema_version": result.get("schema_version"),
        "simulator_decisions_source": simulator_decisions_source,
        "separates": ["native_rubric_score", "business_completion", "evaluation_conflict"],
        "arms_compared_under_one_rule_set": sorted(result.get("arms") or {}),
        "phases": phases,
        "totals": totals,
        "clean_comparison_exclusions": sorted({
            f"t{p['task_number']}" for p in phases if not p["comparison_eligible"]}),
        "old_report_unchanged": True,
    }


# ---------------------------------------------------------------------------
# Offline replay of the sealed r5 instances
# ---------------------------------------------------------------------------

#: The sealed instances this repair was opened for. Each entry names the arm, the
#: task, the r5 transcript index and what the recorded evidence shows. They are
#: replay inputs, not golden answers: the replay asserts that the NEW protocol
#: detects/records them, never that the model's original action was right.
SEALED_R5_INSTANCES = (
    {"id": "R-t11-fabricated-payment-with-stop", "arm": "rewrite", "task": 11,
     "transcript_index": 11, "expect": "violation"},
    {"id": "R-t5-fabricated-payment-no-stop", "arm": "rewrite", "task": 5,
     "transcript_index": 14, "expect": "violation"},
    {"id": "R-t7-fabricated-payment-no-stop", "arm": "rewrite", "task": 7,
     "transcript_index": 17, "expect": "violation"},
    {"id": "E-t5-fabricated-payment-with-stop", "arm": "erratum", "task": 5,
     "transcript_index": 35, "expect": "violation"},
    {"id": "E-t6-fabricated-payment-no-stop", "arm": "erratum", "task": 6,
     "transcript_index": 10, "expect": "violation"},
    {"id": "E-t7-fabricated-payment-with-stop", "arm": "erratum", "task": 7,
     "transcript_index": 13, "expect": "violation"},
    {"id": "E-t10-fabricated-payment-with-stop", "arm": "erratum", "task": 10,
     "transcript_index": 14, "expect": "violation"},
    {"id": "R-t8-fabricated-payment-with-stop", "arm": "rewrite", "task": 8,
     "transcript_index": 25, "expect": "violation"},
    {"id": "R-t4-clean-stop", "arm": "rewrite", "task": 4,
     "transcript_index": 13, "expect": "stop_accepted"},
    {"id": "R-t9-clean-stop", "arm": "rewrite", "task": 9,
     "transcript_index": 15, "expect": "stop_accepted"},
)


def _task_of(result: dict, arm: str, number: int) -> dict:
    for record in ((result.get("arms") or {}).get(arm) or {}).get("tasks") or []:
        if record.get("number") == number:
            return record
    raise KeyError(f"no record for arm={arm} task={number}")


def replay_sealed_user_replies(result: dict, *, instances=SEALED_R5_INSTANCES,
                               protocol=USER_SIMULATOR_PROTOCOL) -> list[dict]:
    """Replay the sealed r5 user turns through the NEW validator, offline.

    The incoming agent text is the transcript entry before each user turn, and the
    known order ids are the phase's own baseline/new orders - the same inputs the
    live driver would have. Nothing is sent, generated or modified.
    """
    out = []
    for case in instances:
        record = _task_of(result, case["arm"], case["task"])
        transcript = record.get("transcript") or []
        index = case["transcript_index"]
        message = transcript[index] if 0 <= index < len(transcript) else {}
        incoming = ""
        for back in range(index - 1, -1, -1):
            if (transcript[back] or {}).get("role") == "assistant":
                incoming = str((transcript[back] or {}).get("content") or "")
                break
        snapshot = record.get("native_snapshot") or {}
        orders = ((snapshot.get("environment_db") or {}).get("orders")) or {}
        baseline = record.get("baseline_order_ids") or []
        new_orders = sorted(new_orders_of(orders, baseline))
        paid = sorted(paid_orders_of(orders, baseline))
        text = message.get("content")
        decision = validate_user_reply(
            text=text, incoming_assistant_text=incoming, new_order_ids=new_orders,
            paid_order_ids=paid, protocol=protocol)
        out.append({
            "id": case["id"], "arm": case["arm"], "task": case["task"],
            "transcript_index": index, "expect": case["expect"],
            "role": message.get("role"),
            "termination_reason": record.get("termination_reason"),
            "native_reward": ((record.get("judge") or {}).get("reward_info") or {}).get("reward"),
            "phase_new_order_ids": new_orders, "phase_paid_order_ids": paid,
            "decision": decision,
        })
    return out


def replay_decisions_by_phase(replay_rows) -> dict:
    """Turn offline replay rows into the per-phase decision map a report consumes.

    These decisions were NOT recorded during the sealed run: they are what the new
    validator says about the same, already-sealed turns. The report labels their
    source so a reader can never mistake them for live capture.
    """
    out: dict[str, dict] = {}
    for row in replay_rows:
        key = f"{row['arm']}:sub_{USER_ID}_{row['task']}"
        entry = out.setdefault(key, {"protocol": row["decision"]["protocol"],
                                     "recorded": False,
                                     "replayed_offline": True, "exchanges": []})
        entry["exchanges"].append(row["decision"])
    for entry in out.values():
        outcomes = [x.get("outcome") for x in entry["exchanges"]]
        entry["outcome"] = ("SIMULATOR_PROTOCOL_VIOLATION"
                            if "SIMULATOR_PROTOCOL_VIOLATION" in outcomes
                            else (outcomes[-1] if outcomes else "NOT_RECORDED"))
        entry["violations"] = [v for x in entry["exchanges"]
                               for v in x.get("violations") or []]
        entry["unverified_claims"] = [c for x in entry["exchanges"]
                                      for c in x.get("claims") or []]
    return out


def evaluate_sealed_r5_run(result: dict, *, scope: dict | None = None,
                           public_scope: dict | None = None,
                           simulator_decisions: dict | None = None,
                           simulator_decisions_source: str = "not_supplied",
                           bundle: dict | None = None) -> dict:
    """Score a loaded sealed r5 run record with the new protocol, offline.

    `scope` carries the tasks' own standard material and `public_scope` the run's
    published rubric list; both are optional. The r5 result carries its own per-task
    judge material. No file is written, no network is touched and r5 is NOT reported
    as re-executed.
    """
    return build_evaluation_report(result, scope=scope, public_scope=public_scope,
                                   simulator_decisions=simulator_decisions,
                                   simulator_decisions_source=simulator_decisions_source,
                                   bundle=bundle)
