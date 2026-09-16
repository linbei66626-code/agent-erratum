"""Read-only cloud transport journal check; NOT task-input or scientific validity."""
import base64
import hashlib
import json
from pathlib import Path

from ae_cloud_proxy import (BYTE_GATE_COUNT_BASIS, CloudConfig, PROFILE, COMPAT_PROFILE,
                           PROFILES, ORIGIN, normalize_request, profile_of,
                           response_summary, source_hashes)
from ae_model_proxy import decode_object, wire_record


def check_capacity_identity(check, normalized_raw: bytes, client: dict, request_id: str) -> None:
    """Reject a `capacity_check` that does not describe the request it sits behind.

    A check is evidence only if it names THIS request (id), was produced for THIS
    role, counted THESE bytes (digest of the normalized body), and carries the
    arithmetic and the counting basis it claims. Anything else is refused instead of
    being read as a passing check.
    """
    if check.get("request_id") != request_id:
        raise ValueError("capacity_check_of_another_request")
    if check.get("input_sha256") != hashlib.sha256(normalized_raw).hexdigest():
        raise ValueError("capacity_check_does_not_describe_this_request")
    if check.get("role") != client.get("role"):
        raise ValueError("capacity_check_role_differs_from_the_request_role")
    if check.get("capacity_basis") == BYTE_GATE_COUNT_BASIS:
        # A byte-gate row carries NO token count: its arithmetic is the measured body
        # against the declared request-byte budget, and the token rule must never be
        # applied to it (nor a token number accepted from it).
        measured = check.get("input_bytes")
        budget = check.get("request_byte_budget")
        if type(measured) is not int or type(budget) is not int or budget <= 0:
            raise ValueError("capacity_check_without_a_byte_measurement")
        if check.get("fits") is not (measured <= budget):
            raise ValueError("capacity_check_arithmetic_inconsistent")
        if check.get("input_tokens") is not None:
            raise ValueError("byte_gate_record_carries_a_token_count")
    else:
        if check.get("fits") is not (check.get("input_tokens", 0)
                                     + check.get("output_reserve_tokens", 0)
                                     <= check.get("context_window", 0)):
            raise ValueError("capacity_check_arithmetic_inconsistent")
        if check.get("input_tokens") is not None and not isinstance(check.get("input_tokens"), int):
            raise ValueError("capacity_check_input_not_an_integer")
    if not isinstance(check.get("count_source"), str) or not check["count_source"]:
        raise ValueError("capacity_check_without_a_count_source")


#: The fields a BYTE gate is audited by. They are absent from a token-gate row, so the
#: projection below stays exactly its old shape for every sealed protocol.
BYTE_CHECK_FIELDS = ("capacity_basis", "input_bytes", "request_byte_budget",
                     "operation_guard_not_a_capacity_guarantee", "token_count_available")


def carry_byte_fields(projected: dict, check: dict) -> dict:
    """Keep a byte-gate row's own evidence in the per-request projection.

    The projection is a summary of the journal, not a replacement for it: dropping the
    byte fields here would leave a byte-gate run's evidence unreadable to the audit that
    has to compare it with the driver's attested rows.
    """
    for name in BYTE_CHECK_FIELDS:
        if name in check:
            projected[name] = check[name]
    if check.get("capacity_basis") == BYTE_GATE_COUNT_BASIS:
        # A byte row has no token count at all, so the projection must not carry the
        # token field - not even as a null - or a reader could not tell a byte record
        # from a token record that failed to report its number.
        projected.pop("input_tokens", None)
    return projected


def audit_cloud_journal(path: Path) -> dict:
    import math
    issues = []
    calls = 0
    # Defined before the try so a failure path cannot turn the report into an
    # UnboundLocalError: a walk that never reached the gate reports "not armed".
    checks = []
    armed = []
    gated = False
    latched = False
    reported_prompt_tokens = 0
    reported_completion_tokens = 0
    profile = None
    try:
        records = [decode_object(line) for line in path.read_bytes().splitlines()]
        if len(records) < 2 or records[0].get("kind") != "cloud_open" or records[-1].get("kind") != "cloud_close":
            raise ValueError("missing_open_or_close")
        if [r.get("sequence") for r in records] != list(range(len(records))):
            raise ValueError("sequence_mismatch")
        opened = records[0]
        profile = opened.get("profile")
        if profile not in PROFILES or opened.get("server_tokenizer_available") is not False:
            raise ValueError("wrong_cloud_profile")
        config = CloudConfig(**opened["config"])
        config.validate()
        declared = profile_of(config)
        if config.profile != profile:
            raise ValueError("profile_config_mismatch")
        if opened.get("code_sha256") != source_hashes():
            raise ValueError("proxy_source_changed_use_matching_version")
        if opened.get("automatic_retry") is not False or opened.get("scientific_result") is not None:
            raise ValueError("wrong_evidence_boundary")
        # A latched failure seals the journal with `blocked: true`. That is still an
        # issue - the capture is not a completed run - but it is recorded AFTER the
        # walk, so the per-request evidence the run did produce stays readable. A
        # declared-capacity stop is exactly that case: refused before sending, with the
        # work that finished before it kept.
        latched = records[-1].get("blocked") is not False
        wire = {}
        for index, record in enumerate(records):
            if "body_base64" in record:
                raw = base64.b64decode(record["body_base64"], validate=True)
                expected = wire_record(raw)
                observed = {k: record[k] for k in ("body_base64", "body_bytes", "body_sha256", "body_utf8") if k in record}
                if observed != expected:
                    raise ValueError("wire_bytes_or_hash_mismatch")
                wire[index] = raw
        cursor = 1
        seen = set()
        client_requests = 0
        last_send = None
        armed = [record for record in records if record.get("kind") == "capacity_armed"]
        if len(armed) > 1:
            raise ValueError("duplicate_capacity_armed_record")
        gated = bool(armed)
        # The proxy's own gate may add records BETWEEN requests (the arming record
        # before the first one). They are skipped by kind, and the per-request
        # `capacity_check` is handled inside each request group below.
        inter_request = ("capacity_armed",)
        while cursor < len(records) - 1:
            while cursor < len(records) - 1 and records[cursor].get("kind") in inter_request:
                cursor += 1
            if cursor >= len(records) - 1:
                break
            client = records[cursor]
            rid = client.get("request_id")
            if client.get("kind") != "client_request" or not isinstance(rid, str) or rid in seen:
                raise ValueError("unexpected_or_duplicate_client_event")
            seen.add(rid)
            client_requests += 1
            method, route = client.get("method"), client.get("path")
            is_chat = (method, route) == ("POST", "/v1/chat/completions")
            if not is_chat and (method, route) != ("GET", "/v1/models"):
                raise ValueError("unexpected_route")
            paced = is_chat and float(config.pace_seconds) > 0
            # The manifest of ONE request, in the order the proxy really writes it:
            # the built body, the gate's own evidence for that body, the pace wait and
            # the send. The two gated rows exist only where a gate was armed.
            kinds = ["client_request"]
            if is_chat:
                kinds.append("normalized_request")
                if gated:
                    kinds.append("capacity_check")
                if paced:
                    kinds.append("pace_wait")
            kinds += ["upstream_request", "upstream_response"]
            if is_chat:
                kinds.append("cloud_summary")
            # The group is "this request, up to the next one": a request refused by the
            # pre-send gate ends with its `blocked` row instead of a send, and the
            # models listing (never gated) is the shortest group of all.
            end = cursor + 1
            while end < len(records) - 1 and records[end].get("kind") != "client_request":
                end += 1
            body = records[cursor + 1:end]
            expected_kinds = kinds[1:]
            # The proxy's HTTP handler annotates a refused response with its own
            # `client_rejected` row: same code, no request id, and it belongs to THIS
            # request's refusal rather than to a second request. It is stripped here,
            # after being required to agree with the refusal it annotates.
            rejected = []
            while body and body[-1].get("kind") == "client_rejected":
                rejected.append(body.pop())
            if rejected:
                if not body or body[-1].get("kind") != "blocked":
                    raise ValueError("client_rejection_without_a_blocked_request")
                if any(row.get("code") != body[-1].get("code") for row in rejected):
                    raise ValueError("client_rejection_code_differs_from_the_blocked_row")
            if any(record.get("request_id") != rid for record in body):
                raise ValueError("request_record_of_another_request")
            if body and body[-1].get("kind") == "blocked":
                # A request the DECLARED gate refused before sending is a complete
                # record of this protocol: the refusal is the last thing written for
                # it, and every row the gate reached is already there. The sealed
                # protocols arm no gate, so for them a refusal stays exactly what it
                # always was: no completed capture.
                if not gated:
                    raise ValueError("blocked_request_without_a_declared_gate")
                if [record.get("kind") for record in body[:-1]] != expected_kinds[:len(body) - 1]:
                    raise ValueError("incomplete_or_reordered_request_events")
                check = next((record for record in body[:-1]
                              if record.get("kind") == "capacity_check"), None)
                if check is not None:
                    check_capacity_identity(check, wire[cursor + 1], client, rid)
                elif not (is_chat and str(body[-1].get("code", "")).startswith("capacity_")):
                    # A refusal with no count is only legitimate when the gate itself
                    # refused to count; any other refusal has to show its numbers.
                    raise ValueError("blocked_request_without_capacity_evidence")
                checks.append(carry_byte_fields(
                    {"request_id": rid, "refused": True,
                     "code": body[-1].get("code"),
                     "role": check.get("role") if check else None,
                     "input_tokens": check.get("input_tokens") if check else None,
                     "output_reserve_tokens": (check.get("output_reserve_tokens")
                                               if check else None),
                     "context_window": check.get("context_window") if check else None,
                     "count_source": check.get("count_source") if check else None,
                     "fits": check.get("fits") if check else False,
                     "input_sha256": check.get("input_sha256") if check else None,
                     "journal_position": cursor},
                    check or {}))
                cursor = end
                continue
            if [record.get("kind") for record in body] != expected_kinds:
                raise ValueError("incomplete_or_reordered_request_events")
            index = {kind: cursor + 1 + position
                     for position, kind in enumerate(expected_kinds)}
            if is_chat and gated:
                check = body[expected_kinds.index("capacity_check")]
                check_capacity_identity(check, wire[index["normalized_request"]], client, rid)
                checks.append(carry_byte_fields(
                    {"request_id": rid, "role": check.get("role"),
                     "input_tokens": check.get("input_tokens"),
                     "output_reserve_tokens": check.get("output_reserve_tokens"),
                     "context_window": check.get("context_window"),
                     "count_source": check.get("count_source"),
                     "fits": check.get("fits"), "refused": False,
                     "input_sha256": check.get("input_sha256"),
                     "journal_position": cursor}, check))
            request_index = index["upstream_request"]
            response_index = index["upstream_response"]
            up, response = records[request_index], records[response_index]
            expected_path = declared.upstream_paths[method]
            if ((up.get("origin"), up.get("method"), up.get("path"))
                    != (declared.origin, method, expected_path)):
                raise ValueError("upstream_route_mismatch")
            if response.get("complete_body") is not True or response.get("http_status") != 200:
                raise ValueError("incomplete_or_failed_response")
            if len(wire[cursor]) > config.max_request_bytes or len(wire[response_index]) > config.max_response_bytes:
                raise ValueError("byte_limit_exceeded")
            if is_chat:
                if paced:
                    pace = body[expected_kinds.index("pace_wait")]
                    if pace.get("kind") != "pace_wait" or pace.get("request_id") != rid:
                        raise ValueError("missing_or_mismatched_pace_event")
                    interval = float(config.pace_seconds)
                    if pace.get("interval_seconds") != interval:
                        raise ValueError("pace_interval_mismatch")
                    if pace.get("first_send") is not (last_send is None):
                        raise ValueError("pace_first_send_mismatch")
                    if pace.get("previous_send_monotonic") != last_send:
                        raise ValueError("pace_previous_send_mismatch")
                    start_time = pace.get("wait_start_monotonic")
                    target = pace.get("target_monotonic")
                    waited = pace.get("waited_seconds")
                    scheduled = pace.get("scheduled_wait_seconds")
                    send = pace.get("send_monotonic")
                    numbers = (start_time, target, waited, scheduled, send)
                    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in numbers)
                            or waited < 0 or scheduled < 0):
                        raise ValueError("pace_wait_time_inconsistent")
                    expected_target = (start_time + interval if last_send is None
                                       else last_send + interval)
                    if abs(target - expected_target) > 1e-6:
                        raise ValueError("pace_target_not_derived_from_interval")
                    if abs(scheduled - max(0.0, expected_target - start_time)) > 1e-6:
                        raise ValueError("pace_scheduled_wait_inconsistent")
                    if abs((send - start_time) - waited) > 1e-6:
                        raise ValueError("pace_waited_not_measured")
                    if send + 1e-6 < expected_target:
                        raise ValueError("pace_sent_before_target")
                    last_send = send
                normalized, changes = normalize_request(wire[cursor], config)
                normalized_record = body[expected_kinds.index("normalized_request")]
                if (normalized != wire[index["normalized_request"]]
                        or normalized != wire[request_index]
                        or normalized_record.get("changes") != changes):
                    raise ValueError("unaccounted_request_change")
                # The fixed routing block is part of the counted/sent request, so it is
                # checked on the RECOMPUTED body: a tampered route fails here.
                routing = declared.routing_block()
                if routing is not None:
                    sent = decode_object(normalized)
                    if sent.get("provider") != routing:
                        raise ValueError("cloud_provider_route_changed")
                    if sent.get("model") != declared.wire_model:
                        raise ValueError("cloud_wire_model_changed")
                expected = response_summary(wire[response_index], 200, decode_object(normalized)["max_tokens"], config)
                summary = body[expected_kinds.index("cloud_summary")]
                if summary is None:
                    raise ValueError("missing_cloud_summary")
                if declared.response_provider_field is not None:
                    # The RESPONDER's identity is checked before the generic summary
                    # comparison, so a missing or substituted provider is reported as
                    # exactly that instead of as a summary mismatch.
                    provider_identity = summary.get("response_provider")
                    if not isinstance(provider_identity, str) or not provider_identity:
                        raise ValueError("missing_response_provider_identity")
                    if (declared.provider_slug is not None
                            and provider_identity.lower() != declared.provider_slug.lower()):
                        raise ValueError("unexpected_response_provider")
                if any(k not in summary or summary[k] != v for k, v in expected.items()) or expected["transport_issues"]:
                    raise ValueError("response_summary_or_transport_issue")
                if any(summary.get(k) != client.get(k) for k in ("purpose", "role")):
                    raise ValueError("request_attribution_mismatch")
                if declared.response_provider_field is not None:
                    provider_identity = summary.get("response_provider")
                    if not isinstance(provider_identity, str) or not provider_identity:
                        raise ValueError("missing_response_provider_identity")
                    if (declared.provider_slug is not None
                            and provider_identity.lower() != declared.provider_slug.lower()):
                        raise ValueError("unexpected_response_provider")
                    if summary.get("response_model") != declared.wire_model:
                        raise ValueError("unexpected_response_model")
                calls += 1
                reported_prompt_tokens += expected["usage"]["prompt_tokens"]
                reported_completion_tokens += expected["usage"]["completion_tokens"]
            elif wire[cursor] or wire[request_index]:
                raise ValueError("nonempty_models_request")
            cursor = end
        if cursor != len(records) - 1 or records[-1].get("request_count") != client_requests:
            raise ValueError("request_count_mismatch")
        if not 0 < client_requests <= config.max_requests:
            raise ValueError("request_limit_or_empty_capture")
    except Exception as exc:
        # Never reproduce arbitrary private body/error strings into this report.
        known = str(exc) if isinstance(exc, ValueError) else "malformed_capture"
        issues.append(known if known.replace("_", "").isalnum() and len(known) <= 80 else "malformed_capture")
    if latched:
        issues.append("proxy_latched_failure")
    # A gated capture must carry exactly one check per chat request; a missing or
    # duplicated check is a defect of the pre-send gate, not a detail.
    if gated:
        chat_requests = [record for record in records
                         if record.get("kind") == "client_request"
                         and (record.get("method"), record.get("path"))
                         == ("POST", "/v1/chat/completions")]
        if len(checks) != len(chat_requests):
            issues.append("capacity_checks_differ_from_chat_requests")
        elif [check["request_id"] for check in checks] != [request["request_id"]
                                                           for request in chat_requests]:
            issues.append("capacity_checks_do_not_follow_the_requests")
    return {"profile": profile, "transport_capture_checked": not issues, "issues": issues,
            "completed_chat_requests_checked": calls,
            "capacity_armed": gated,
            "capacity_declaration_sha256": (armed[0].get("declaration_sha256") if gated else None),
            "capacity_checks": checks,
            "provider_reported_prompt_tokens_checked": reported_prompt_tokens,
            "provider_reported_completion_tokens_checked": reported_completion_tokens,
            "token_ids": None, "exact_prefix_reuse_verified": False,
            "task_input_audit_passed": None, "scientific_result": None,
            "note": "Structural transport check only; no answer/tool-execution correctness or independent token proof."}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    args = parser.parse_args()
    result = audit_cloud_journal(args.journal)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["transport_capture_checked"] else 2)
