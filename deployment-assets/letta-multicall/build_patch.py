#!/usr/bin/env python3
"""Generate the AE multicall Letta patch from a scratch checkout copy.

Deterministic: every replacement is an exact single-occurrence string. Run from
the project root:

    python deployment-assets/letta-multicall/build_patch.py /tmp/ae-multicall-patch/letta-v1

It rewrites the two pinned Letta files in place and prints the unified diff to
stdout. It never touches the pinned original checkout.
"""
from __future__ import annotations

from pathlib import Path
import sys

EXPECTED_HEAD = "56ba9c25552605eec89de8ed3dc6394b625c1993"

MESSAGE_IMPORT_OLD = (
    "from letta.helpers.datetime_helpers import get_utc_time, is_utc_datetime\n"
)
MESSAGE_IMPORT_NEW = (
    "from letta.helpers.datetime_helpers import get_utc_time, is_utc_datetime\n"
    "# AE-MULTICALL: opt-in receive compatibility for multiple client tool calls\n"
    "# and full-length tool-call ids. See letta/helpers/ae_multicall_compat.py.\n"
    "from letta.helpers.ae_multicall_compat import preserve_tool_call_id, sanitize_tool_call_id_compat\n"
)

MESSAGE_EDITS = (
    (
        '                if max_tool_id_length:\n'
        '                    for tool_call_dict in openai_message["tool_calls"]:\n'
        '                        tool_call_dict["id"] = tool_call_dict["id"][:max_tool_id_length]\n',
        '                if max_tool_id_length:\n'
        '                    for tool_call_dict in openai_message["tool_calls"]:\n'
        '                        # AE-MULTICALL: keep the provider id whole under the profile.\n'
        '                        tool_call_dict["id"] = preserve_tool_call_id(\n'
        '                            tool_call_dict["id"], max_tool_id_length\n'
        '                        )\n',
    ),
    (
        '                            "tool_call_id": tr.tool_call_id[:max_tool_id_length] if max_tool_id_length else tr.tool_call_id,\n',
        '                            "tool_call_id": preserve_tool_call_id(tr.tool_call_id, max_tool_id_length),\n',
    ),
    (
        '                    "tool_call_id": tool_return.tool_call_id[:max_tool_id_length] if max_tool_id_length else tool_return.tool_call_id,\n',
        '                    "tool_call_id": preserve_tool_call_id(tool_return.tool_call_id, max_tool_id_length),\n',
    ),
    (
        '                    "tool_call_id": self.tool_call_id[:max_tool_id_length] if max_tool_id_length else self.tool_call_id,\n',
        '                    "tool_call_id": preserve_tool_call_id(self.tool_call_id, max_tool_id_length),\n',
    ),
    (
        '                            "id": sanitize_tool_call_id(tool_call.id),\n',
        '                            "id": sanitize_tool_call_id_compat(tool_call.id),\n',
    ),
    (
        '                            "tool_use_id": sanitize_tool_call_id(resolved_tool_call_id),\n',
        '                            "tool_use_id": sanitize_tool_call_id_compat(resolved_tool_call_id),\n',
    ),
)

AGENT_OLD = (
    "                # Enforce parallel_tool_calls=false by truncating to first tool call\n"
    "                # Some providers (e.g. Gemini) don't respect this setting via API, so we enforce it client-side\n"
    "                if len(tool_calls) > 1 and not active_llm_config.parallel_tool_calls:\n"
    "                    self.logger.warning(\n"
    "                        f\"LLM returned {len(tool_calls)} tool calls but parallel_tool_calls=false. \"\n"
    "                        f\"Truncating to first tool call: {tool_calls[0].function.name}\"\n"
    "                    )\n"
    "                    tool_calls = [tool_calls[0]]\n"
    "\n"
)
AGENT_NEW = (
    "                # Enforce parallel_tool_calls=false by truncating to first tool call\n"
    "                # Some providers (e.g. Gemini) don't respect this setting via API, so we enforce it client-side\n"
    "                # AE-MULTICALL: the decision is taken HERE, while the received call list\n"
    "                # is still complete - i.e. before any truncation. Three outcomes:\n"
    "                #   * profile not declared -> upstream truncation runs unchanged;\n"
    "                #   * profile declared and the strict gate verifies -> keep every\n"
    "                #     declared client tool call for serial client-side execution;\n"
    "                #   * profile declared but unverifiable/unknown/unsupported ->\n"
    "                #     MulticallRefused is raised, so the step stops with the raw\n"
    "                #     response intact and NO tool side effect. Falling back to\n"
    "                #     truncation here would silently drop calls.\n"
    "                if len(tool_calls) > 1 and not active_llm_config.parallel_tool_calls:\n"
    "                    from letta.helpers.ae_multicall_compat import (\n"
    "                        KEEP_ALL,\n"
    "                        MulticallRefused,\n"
    "                        NOT_DECLARED,\n"
    "                        multicall_decision,\n"
    "                    )\n"
    "\n"
    "                    _ae_decision = multicall_decision(tool_calls, self.client_tools)\n"
    "                    if _ae_decision == KEEP_ALL:\n"
    "                        self.logger.info(\n"
    "                            f\"AE-MULTICALL kept {len(tool_calls)} declared client tool \"\n"
    "                            \"calls for serial client-side execution\"\n"
    "                        )\n"
    "                    elif _ae_decision != NOT_DECLARED:\n"
    "                        raise MulticallRefused(\n"
    "                            f\"unexpected multicall decision {_ae_decision!r}\")\n"
    "                    else:\n"
    "                        self.logger.warning(\n"
    "                            f\"LLM returned {len(tool_calls)} tool calls but parallel_tool_calls=false. \"\n"
    "                            f\"Truncating to first tool call: {tool_calls[0].function.name}\"\n"
    "                        )\n"
    "                        tool_calls = [tool_calls[0]]\n"
    "\n"
)


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def main(argv) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    message = root / "letta/schemas/message.py"
    agent = root / "letta/agents/letta_agent_v3.py"
    compat = root / "letta/helpers/ae_multicall_compat.py"
    for path in (message, agent, compat):
        if not path.is_file():
            raise SystemExit(f"missing required file: {path}")
    replace_once(message, MESSAGE_IMPORT_OLD, MESSAGE_IMPORT_NEW)
    for old, new in MESSAGE_EDITS:
        replace_once(message, old, new)
    replace_once(agent, AGENT_OLD, AGENT_NEW)
    print(f"PATCHED {message}")
    print(f"PATCHED {agent}")
    print(f"NEW     {compat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
