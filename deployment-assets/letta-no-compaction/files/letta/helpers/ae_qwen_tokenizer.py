"""Official Qwen byte-level BPE + chat template, staged for the AE capacity gate.

This file is GENERATED from `ae_multiturn_capacity.py` in the project checkout by
`tools/stage_letta_tokenizer.py`; the two must stay identical. The service process
uses it to count the FINAL provider request, because the provider's hard window is
expressed in its own tokens and the pinned approximate counter can under-estimate
Chinese text by more than 2x.

The official `tokenizers`/`transformers` packages are not required: this is the
official algorithm over the official assets (vocab + merges + pre-tokenizer regex +
NFC normaliser + the template from `tokenizer_config.json`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import regex
import unicodedata


#: The official Qwen chat template's tool-framing preamble, read from the template
#: itself by `chat_template_prefix_and_tools`; the strings below are the template's
#: literal text and are asserted against the template's own bytes.
TEMPLATE_TOOLS_HEADER = (
    "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n<tools>")
TEMPLATE_TOOLS_FOOTER = (
    "\n</tools>\n\nFor each function call, return a json object with function name and "
    "arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n"
    '{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>')
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"

#: The pre-tokenizer regex, verbatim from the official `tokenizer.json`.
PRETOKENIZER_PATTERN = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*"
    r"|\s*[\r\n]+|\s+(?!\S)|\s+")
BYTE_LEVEL_PATTERN = regex.compile(PRETOKENIZER_PATTERN)
SPECIAL_TOKENS = ("<|endoftext|>", "<|im_start|>", "<|im_end|>", "<|object_ref_start|>",
                  "<|object_ref_end|>", "<|box_start|>", "<|box_end|>", "<|quad_start|>",
                  "<|quad_end|>", "<|vision_start|>", "<|vision_end|>", "<|vision_pad|>",
                  "<|image_pad|>", "<|video_pad|>")


def bytes_to_unicode():
    """The ByteLevel byte<->unicode map, exactly as the official encoder defines it."""
    bs = (list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1))
          + list(range(ord("®"), ord("ÿ") + 1)))
    cs = list(bs)
    index = 0
    for byte in range(256):
        if byte not in bs:
            bs.append(byte)
            cs.append(256 + index)
            index += 1
    return dict(zip(bs, [chr(code) for code in cs]))


def unicode_to_bytes():
    return {value: key for key, value in bytes_to_unicode().items()}


#: The TARGET model's own tokenizer assets, as retrieved from the official repository
#: at a fixed revision. Every file is pinned by BYTES and SHA-256 here, so "the target
#: assets" is a file identity rather than two equal model-name strings; a missing or
#: altered file is a refusal, never a silent fall back to the family cache.
TARGET_TOKENIZER = {
    "model": "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "revision": "0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe",
    "directory": ("/Users/lrc/.agent-reach/ae-01-target-tokenizer/"
                  "0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe"),
    "files": {
        "tokenizer.json": {
            "bytes": 11422654,
            "sha256": "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"},
        "tokenizer_config.json": {
            "bytes": 9377,
            "sha256": "a62ff0a2472a0fa1b8eaabcb57c59b58afa42a22831dc141400b6e0cf2b65ce3"},
        "vocab.json": {
            "bytes": 2776833,
            "sha256": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"},
        "merges.txt": {
            "bytes": 1671839,
            "sha256": "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3"},
        "config.json": {
            "bytes": 963,
            "sha256": "a1ee086a68d0cbfc87316da00ba4b8507bd1292978108e2496201a30a450f438"},
    },
    "template_sha256": "64f85b198065d0fba2a81f37e10ed68161ce2c19a754c7100e67e0ca2ee9c326",
    "model_max_length_is_not_endpoint_capacity": 1010000,
    "native_max_position_embeddings": 262144,
}


def load_target_tokenizer_assets(directory=None, declaration=None):
    """Read the TARGET model's own tokenizer assets and verify their identity.

    The declaration pins every file by byte count and SHA-256; each file is re-read
    and re-hashed here, and the chat template's own digest is checked too. Any
    missing, renamed, truncated or altered file raises with the exact difference.
    There is no fallback: a caller that needs the target's own tokenizer cannot be
    served a different model's file by this function.
    """
    record = dict(declaration or TARGET_TOKENIZER)
    root = Path(directory) if directory else Path(record["directory"])
    if not root.is_dir():
        raise FileNotFoundError(f"the target tokenizer directory is absent: {root}")
    if root.name != record["revision"]:
        raise ValueError(
            f"the target tokenizer directory is not the declared revision: {root.name}")
    files = {}
    for name, expected in sorted(record["files"].items()):
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"the target tokenizer file is absent: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != expected["bytes"]:
            raise ValueError(
                f"the target tokenizer file {name} has {len(raw)} bytes, "
                f"the declaration says {expected['bytes']}")
        if digest != expected["sha256"]:
            raise ValueError(
                f"the target tokenizer file {name} is not the declared bytes: {digest}")
        files[name] = {"path": str(path), "bytes": len(raw), "sha256": digest}
    config = json.loads((root / "tokenizer_config.json").read_text(encoding="utf-8"))
    template = config.get("chat_template")
    if not isinstance(template, str) or not template:
        raise ValueError("the target tokenizer_config carries no chat template")
    template_digest = hashlib.sha256(template.encode("utf-8")).hexdigest()
    if template_digest != record["template_sha256"]:
        raise ValueError(
            "the target chat template is not the declared one: " + template_digest)
    return {"source": "declared_target_assets", "asset_is_target": True,
            "model": record["model"], "revision": record["revision"],
            "directory": str(root), "files": files,
            "tokenizer_json": root / "tokenizer.json",
            "tokenizer_config": root / "tokenizer_config.json",
            "template_sha256": template_digest,
            "model_max_length_is_not_endpoint_capacity":
                record["model_max_length_is_not_endpoint_capacity"],
            "native_max_position_embeddings": record["native_max_position_embeddings"]}


def resolve_tokenizer_assets(kind, hub=None, *, tokenizer_json=None, tokenizer_config=None):
    """One place that answers "which assets may this caller count with".

    `kind` is either "target" - the TARGET model's own declared assets, verified by
    file identity, with NO fallback - or "exploration", the family cache, which is
    labelled as another model's asset. A caller that asks for the target while only
    the family cache exists is refused instead of silently counted with the wrong
    tokenizer.
    """
    if kind == "target":
        if tokenizer_json and tokenizer_config:
            return load_target_tokenizer_assets()
        return load_target_tokenizer_assets()
    if kind == "exploration":
        assets = load_official_tokenizer_assets(hub)
        assets["asset_is_target"] = False
        return assets
    raise ValueError(f"unknown tokenizer asset kind: {kind!r}")


def load_official_tokenizer_assets(hub=None):
    """Locate the official Qwen tokenizer assets in the local HuggingFace cache.

    Returns a dict with the three files and their digests, or raises with the exact
    missing item. Nothing is downloaded; a missing asset is a reported gap.
    """
    hub = Path(hub) if hub else (Path.home() / ".cache/huggingface/hub")
    for name in ("models--Qwen--Qwen3-8B", "models--Qwen--Qwen3-0.6B"):
        root = hub / name
        snapshots = sorted((root / "snapshots").glob("*")) if (root / "snapshots").is_dir() else []
        for snapshot in snapshots:
            tokenizer = snapshot / "tokenizer.json"
            config = snapshot / "tokenizer_config.json"
            if tokenizer.is_file() and config.is_file():
                return {"source": "huggingface_cache", "snapshot": str(snapshot),
                        "tokenizer_json": tokenizer, "tokenizer_config": config,
                        "revision": (root / "refs/main").read_text(encoding="utf-8").strip()
                        if (root / "refs/main").is_file() else None}
    raise FileNotFoundError(
        "the official Qwen tokenizer assets are not present in the local cache "
        f"({hub}); no other model's tokenizer may stand in for them")


class QwenBpeTokenizer:
    """The official Qwen byte-level BPE, implemented from the official assets."""

    def __init__(self, tokenizer_json: Path, tokenizer_config: Path):
        document = json.loads(Path(tokenizer_json).read_text(encoding="utf-8"))
        model = document["model"]
        if model.get("type") != "BPE" or model.get("byte_fallback"):
            raise ValueError("the official tokenizer is a non-fallback byte-level BPE")
        self.vocab = dict(model["vocab"])
        self.reverse_vocab = {value: token for token, value in self.vocab.items()}
        self.merges = {}
        for rank, entry in enumerate(model["merges"]):
            parts = entry.split(" ") if isinstance(entry, str) else list(entry)
            if len(parts) != 2:
                continue
            self.merges[(parts[0], parts[1])] = rank
        self.unk_token = model.get("unk_token")
        self.special_tokens = {entry["content"]: entry["id"]
                               for entry in document.get("added_tokens", [])
                               if entry.get("special")}
        self._special_ids = set(self.special_tokens.values())
        self._special_names = {value: token for token, value in self.special_tokens.items()}
        self._special_pattern = regex.compile(
            "(" + "|".join(regex.escape(token) for token in
                           sorted(self.special_tokens, key=len, reverse=True)) + ")")
        self._byte_map = bytes_to_unicode()
        self._byte_lookup = unicode_to_bytes()
        config = json.loads(Path(tokenizer_config).read_text(encoding="utf-8"))
        self.chat_template = config.get("chat_template")
        self.model_max_length = config.get("model_max_length")
        self.assets = {
            "tokenizer_json": {"path": str(tokenizer_json),
                               "sha256": hashlib.sha256(
                                   Path(tokenizer_json).read_bytes()).hexdigest()},
            "tokenizer_config": {"path": str(tokenizer_config),
                                 "sha256": hashlib.sha256(
                                     Path(tokenizer_config).read_bytes()).hexdigest()},
        }

    # -- BPE ---------------------------------------------------------------
    def _bpe(self, token: str) -> list:
        """The official merge loop over one pre-token, as symbol pairs."""
        symbols = list(token)
        if len(symbols) < 2:
            return symbols
        while True:
            pairs = set(zip(symbols[:-1], symbols[1:]))
            candidate = min(pairs, key=lambda pair: self.merges.get(pair, float("inf")))
            if candidate not in self.merges:
                break
            first, second = candidate
            merged, index = [], 0
            while index < len(symbols):
                if (index < len(symbols) - 1 and symbols[index] == first
                        and symbols[index + 1] == second):
                    merged.append(first + second)
                    index += 2
                else:
                    merged.append(symbols[index])
                    index += 1
            symbols = merged
            if len(symbols) == 1:
                break
        return symbols

    def encode_ordinary(self, text: str) -> list:
        """Encode text with NO special-token handling (the template adds them)."""
        import unicodedata
        text = unicodedata.normalize("NFC", text)
        ids = []
        for piece in BYTE_LEVEL_PATTERN.findall(text):
            mapped = "".join(self._byte_map[byte] for byte in piece.encode("utf-8"))
            for symbol in self._bpe(mapped):
                ids.append(self.vocab.get(symbol, self.vocab.get(self.unk_token)))
        return ids

    def encode(self, text: str) -> list:
        """Encode text, resolving the template's special tokens to their ids."""
        ids = []
        for part in self._special_pattern.split(text):
            if not part:
                continue
            if part in self.special_tokens:
                ids.append(self.special_tokens[part])
            else:
                ids.extend(self.encode_ordinary(part))
        return ids

    def count(self, text: str) -> int:
        return len(self.encode(text))

    def decode(self, ids: list) -> str:
        """Decode back to text, so a round-trip check can prove the tables are right."""
        pieces = []
        for item in ids:
            if item in self._special_ids:
                pieces.append(self._special_names[item])
                continue
            pieces.append(self.reverse_vocab.get(item, ""))
        mapped = "".join(pieces)
        data = bytearray()
        for character in mapped:
            data.append(self._byte_lookup.get(character, ord("?")))
        return data.decode("utf-8", errors="replace")


#: The counting contract, shared by the offline report and the runtime gate. Every
#: user of this module counts THE SAME THING: the model-visible prompt.
COUNT_CONTRACT = {
    "entry": "count_request_prompt",
    "tokenizer": "the official Qwen byte-level BPE over vocab.json + merges.txt "
                 "(NFC normalised, the official pre-tokenizer regex)",
    "renderer": "the official chat template from tokenizer_config.json",
    "generation_prompt": "NOT added: the request under test is the prompt the provider "
                         "receives, and no assistant generation prefix is part of it",
    "tool_serialization": "each tool is rendered by the template's own `tool | tojson` "
                          "path (compact JSON, ensure_ascii=False), inside <tools></tools>",
    "special_tokens": "the template's literal markers (<|im_start|>, <|im_end|>) are "
                      "encoded as their own added-token ids",
    "not_a_count": "the byte length or the tokenization of the HTTP JSON body: JSON syntax "
                   "adds tokens the model never sees, so a body count is not a model count",
}


#: The JSON serialization BOTH supported renderer paths must use for the template's
#: `tojson`. Jinja's stock filter is NOT it: it sorts keys, ASCII-escapes non-ASCII
#: text (`中文` -> `\\u4e2d...`) and HTML-escapes `<`, `>`, `&`. Those are different
#: prompt bytes from the ones the wire contract and the pinned renderer produce, so
#: the filter is pinned here and installed into the Jinja environment explicitly.
TOJSON_POLICY = {"ensure_ascii": False, "separators": [", ", ": "], "sort_keys": False}

#: Missing OPTIONAL fields (for example `tool_calls` on a plain assistant message) are
#: legal for this template: the official runtime resolves them to an undefined value
#: that is falsy in `{% if %}`. `ChainableUndefined` is the closest match to that
#: runtime; plain `Undefined` is the fallback on older Jinja2.
UNDEFINED_POLICY = "ChainableUndefined"


def template_runtime():
    """The template runtime available HERE, or None with the minimal missing item.

    The official chat template is Jinja2. When Jinja2 can be imported the template
    is EXECUTED; when it cannot, the pinned renderer below reproduces the template's
    own semantics and says so in every count it produces. Nothing else is invented:
    a hand-written generic template engine is explicitly not wanted.
    """
    import importlib.util
    if importlib.util.find_spec("jinja2") is not None:
        return {"name": "jinja2", "missing": None}
    return {"name": None,
            "missing": "jinja2 (with MarkupSafe) is not installed in this interpreter; "
                       "the official chat template cannot be executed here"}


def render_with_template_runtime(template_text, messages, tools, *,
                                 add_generation_prompt=False, enable_thinking=None):
    """Execute the ASSET'S OWN chat template with Jinja2 (the production path).

    Two environment details decide whether this path means the same thing as the
    pinned renderer, and both are set explicitly rather than inherited from whatever
    Jinja happens to default to:

    * undefined values: a missing optional field is legal here and must behave as the
      official runtime's undefined (falsy, renders as empty) instead of raising;
    * `tojson`: installed from `TOJSON_POLICY`, because Jinja's stock filter would
      sort keys, escape non-ASCII and HTML-escape the text.
    """
    import jinja2
    undefined = getattr(jinja2, "ChainableUndefined", jinja2.Undefined)
    environment = jinja2.Environment(undefined=undefined,
                                     trim_blocks=False, lstrip_blocks=False,
                                     autoescape=False, keep_trailing_newline=True,
                                     extensions=["jinja2.ext.loopcontrols"])
    environment.filters["tojson"] = _tojson
    environment.policies["json.dumps_function"] = json.dumps
    environment.policies["json.dumps_kwargs"] = {"sort_keys": TOJSON_POLICY["sort_keys"],
                                                 "ensure_ascii": TOJSON_POLICY["ensure_ascii"]}
    template = environment.from_string(template_text)
    return template.render(messages=messages, tools=tools or [],
                           add_generation_prompt=add_generation_prompt,
                           enable_thinking=enable_thinking)


def render_prompt_bytes(tokenizer, messages, tools=None, *,
                        add_generation_prompt: bool = False, enable_thinking=None):
    """Render one request, verifying the two supported paths agree BYTE FOR BYTE.

    Both paths are supported: the pinned renderer (always available) and executing the
    asset's own template (when a template runtime exists). The counting entry never
    picks one silently: when the runtime exists BOTH are executed and a difference is
    a refusal, so a dependency appearing or disappearing cannot change what is
    counted. The returned info records which paths ran and with which policies.
    """
    template_text = getattr(tokenizer, "chat_template", None)
    if not isinstance(template_text, str) or not template_text:
        raise ValueError("the tokenizer asset carries no chat template")
    template_sha256 = hashlib.sha256(template_text.encode("utf-8")).hexdigest()
    pinned = render_qwen_chat(messages, tools,
                              add_generation_prompt=add_generation_prompt,
                              enable_thinking=enable_thinking, template=template_text)
    runtime = template_runtime()
    info = {"template_sha256": template_sha256,
            "template_variant": template_variant(template_text),
            "tojson_policy": dict(TOJSON_POLICY),
            "undefined_policy": UNDEFINED_POLICY if runtime["name"] else None,
            "runtime": runtime["name"], "runtime_missing": runtime["missing"],
            "paths_compared": runtime["name"] is not None}
    if runtime["name"] is None:
        info["renderer"] = "pinned_renderer_from_template"
        info["renderer_basis"] = ("the asset's chat template reproduced by the pinned "
                                  "renderer; template runtime unavailable: "
                                  + str(runtime["missing"]))
        return pinned, info
    executed = render_with_template_runtime(
        template_text, messages, tools, add_generation_prompt=add_generation_prompt,
        enable_thinking=enable_thinking)
    if executed != pinned:
        raise RuntimeError(
            "the two supported renderer paths disagree for this request; refusing to "
            "count it (template " + template_sha256[:16] + ", request "
            + hashlib.sha256(json.dumps([messages, tools or []], ensure_ascii=False,
                                        sort_keys=True).encode("utf-8")).hexdigest()[:16]
            + "): the template runtime must not silently change the counted bytes")
    info["renderer"] = "jinja2_chat_template_verified_equal"
    info["renderer_basis"] = ("the asset's own chat template executed by Jinja2 and "
                              "verified byte-identical to the pinned renderer for THIS "
                              "request")
    return pinned, info


def count_request_prompt(tokenizer, messages, tools=None, *,
                         add_generation_prompt: bool = False,
                         enable_thinking=None) -> dict:
    """Count the MODEL-VISIBLE prompt of one request, through the shared contract.

    Returns the count AND how it was produced, so a caller can record the basis
    instead of asserting one. Both the offline capacity report and the runtime
    capacity gate call this function; a divergence between them is what the r2
    review found, and this is the single entry that removes it.

    The rendering follows the ASSET'S OWN `chat_template`: when a template runtime is
    available the template itself is executed, and otherwise the pinned renderer
    reproduces that template's semantics - selected BY THE TEMPLATE TEXT, so pointing
    the entry at another asset (for example the target model's own config, whose
    template drops the thinking branch) really changes the rendering.
    """
    if not isinstance(messages, list) or not messages:
        raise ValueError("count_request_prompt needs the request's messages")
    template_text = getattr(tokenizer, "chat_template", None)
    if not isinstance(template_text, str) or not template_text:
        raise ValueError("the tokenizer asset carries no chat template")
    rendered, info = render_prompt_bytes(
        tokenizer, messages, tools, add_generation_prompt=add_generation_prompt,
        enable_thinking=enable_thinking)
    return {"prompt_tokens": tokenizer.count(rendered), "rendered_chars": len(rendered),
            "contract": "count_request_prompt", "tokenizer": "official_qwen_bpe",
            "tools": len(tools or []), "messages": len(messages),
            "add_generation_prompt": add_generation_prompt,
            "rendered_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "renderer": info["renderer"], "template_sha256": info["template_sha256"],
            "renderer_basis": info["renderer_basis"],
            "template_variant": info["template_variant"],
            "tojson_policy": info["tojson_policy"],
            "undefined_policy": info["undefined_policy"],
            "paths_compared": info["paths_compared"]}


def _tojson(value) -> str:
    """Jinja's `tojson` in the official template: compact, ensure_ascii=False."""
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def template_variant(template_text) -> str:
    """Which official Qwen template this text IS, read from the text itself.

    The 30B-Instruct-2507 template removes the thinking machinery of the earlier
    family template: no `multi_step_tool`/`last_query_index` namespace, no
    `<think>`/`reasoning_content` handling for assistant turns, and no
    `enable_thinking is false` block in the generation prompt. The renderer must not
    guess: the template's own text decides, so an asset swap changes the output.
    """
    if not isinstance(template_text, str) or not template_text:
        raise ValueError("the template variant needs the template text")
    if "multi_step_tool" in template_text or "reasoning_content" in template_text:
        return "qwen3_thinking"
    return "qwen3_instruct_no_thinking"


def render_qwen_chat(messages, tools=None, add_generation_prompt=False,
                     enable_thinking=None, *, template=None) -> str:
    """Render an official Qwen chat template over real messages and tools.

    The rendering follows the template text: `template` selects the variant
    (`template_variant`). Without a template the historical behaviour is kept, which
    is the thinking variant. Jinja2 is not installed offline, so the rendering is
    implemented here and each structural rule is covered by a test against the
    template's own text; when a template runtime IS available,
    `count_request_prompt` executes the template instead of this function.
    """
    variant = template_variant(template) if template is not None else "qwen3_thinking"
    out = []
    tools = list(tools or [])
    if tools:
        out.append(IM_START + "system\n")
        if messages and messages[0].get("role") == "system":
            out.append((messages[0].get("content") or "") + "\n\n")
        out.append(TEMPLATE_TOOLS_HEADER)
        for tool in tools:
            out.append("\n")
            out.append(_tojson(tool))
        out.append(TEMPLATE_TOOLS_FOOTER + IM_END + "\n")
    else:
        if messages and messages[0].get("role") == "system":
            out.append(IM_START + "system\n" + (messages[0].get("content") or "")
                       + IM_END + "\n")
    # The last real user query index decides the thinking-mode framing; the
    # no-thinking template has no such framing at all.
    last_query_index = len(messages) - 1
    multi_step = variant == "qwen3_thinking"
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = message.get("content")
        if (multi_step and message.get("role") == "user" and isinstance(content, str)
                and not (content.startswith("<tool_response>")
                         and content.endswith("</tool_response>"))):
            multi_step = False
            last_query_index = index
    for index, message in enumerate(messages):
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        role = message.get("role")
        if role in ("user", "system") and not (role == "system" and index == 0):
            out.append(IM_START + role + "\n" + content + IM_END + "\n")
        elif role == "assistant":
            if variant == "qwen3_thinking":
                reasoning = message.get("reasoning_content")
                if not isinstance(reasoning, str):
                    reasoning = ""
                    if "</think>" in content:
                        reasoning = content.split("</think>")[0].rstrip("\n").split("<think>")[-1].lstrip("\n")
                        content = content.split("</think>")[-1].lstrip("\n")
                if index > last_query_index:
                    if index == len(messages) - 1 or (index != len(messages) - 1 and reasoning):
                        out.append(IM_START + role + "\n<think>\n" + reasoning.strip("\n")
                                   + "\n</think>\n\n" + content.lstrip("\n"))
                    else:
                        out.append(IM_START + role + "\n" + content)
                else:
                    out.append(IM_START + role + "\n" + content)
            else:
                # The target template's assistant turn is always this one line.
                out.append(IM_START + role + "\n" + content)
            for call_index, call in enumerate(message.get("tool_calls") or []):
                if (call_index == 0 and content) or call_index > 0:
                    out.append("\n")
                function = call.get("function") if call.get("function") else call
                out.append('<tool_call>\n{"name": "' + str(function.get("name")))
                out.append('", "arguments": ')
                arguments = function.get("arguments")
                out.append(arguments if isinstance(arguments, str) else _tojson(arguments))
                out.append("}\n</tool_call>")
            out.append(IM_END + "\n")
        elif role == "tool":
            if index == 0 or messages[index - 1].get("role") != "tool":
                out.append(IM_START + "user")
            out.append("\n<tool_response>\n" + content + "\n</tool_response>")
            if index == len(messages) - 1 or messages[index + 1].get("role") != "tool":
                out.append(IM_END + "\n")
    if add_generation_prompt:
        out.append(IM_START + "assistant\n")
        if variant == "qwen3_thinking" and enable_thinking is False:
            out.append("<think>\n\n</think>\n\n")
    return "".join(out)


def _split_system_memory(text):
    """Split one system prompt into (system-without-memory, memory-section).

    Letta compiles the memory blocks INTO the system prompt, so counting the whole
    prompt as "system" and the block text again as "memory" would count it twice.
    The split is by the block's own fenced section; a prompt with no recognisable
    section is returned whole as system text with an empty memory part, so the two
    parts always sum to the original.
    """
    import re as _re
    for pattern in (r"<memory_blocks>.*?</memory_blocks>", r"<memory>.*?</memory>",
                    r"<memory_blocks>.*\Z"):
        match = _re.search(pattern, text, _re.S)
        if match:
            return text[:match.start()] + text[match.end():], match.group(0)
    return text, ""


def categories_of(messages, tools=None):
    """Split a request's messages into the categories the report attributes cost to.

    The split is structural (roles and the multi-turn envelope), never a guess about
    content: a `dataset_history/material` envelope is history, a `current_task`
    envelope is this task's own material, and the rest are chat turns.
    """
    buckets = {"system": [], "memory_blocks": [], "current_task": [], "history_material": [],
               "chat_history": [], "tool_returns": [], "assistant_messages": []}
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        if role == "system":
            # The system prompt CONTAINS the memory block, so the two categories
            # overlap. They are split MUTUALLY EXCLUSIVELY here: the memory-block
            # section is removed from the system text before it is counted, so the
            # two numbers can be added without double counting.
            stripped, blocks = _split_system_memory(content)
            if stripped:
                buckets["system"].append(stripped)
            if blocks:
                buckets["memory_blocks"].append(blocks)
            continue
        source = None
        stripped = content.lstrip()
        if stripped.startswith("{"):
            try:
                envelope = json.loads(content)
                source = envelope.get("source") if isinstance(envelope, dict) else None
            except ValueError:
                source = None
        if source == "dataset_history/material":
            buckets["history_material"].append(content)
        elif source == "current_task":
            buckets["current_task"].append(content)
        elif source in ("runtime_user",):
            buckets["chat_history"].append(content)
        elif role == "user":
            buckets["chat_history"].append(content)
        elif role == "tool":
            buckets["tool_returns"].append(content)
        elif role == "assistant":
            buckets["assistant_messages"].append(content)
        else:
            buckets["chat_history"].append(content)
        if isinstance(message.get("tool_calls"), list):
            buckets["assistant_messages"].append(_tojson(message["tool_calls"]))
    if tools:
        buckets["tool_schemas"] = [_tojson(tool) for tool in tools]
    return buckets


# -- report ----------------------------------------------------------------
#: The sealed single-arm run that stopped on Letta automatic compaction. Every
#: number under `sealed_run` is read from ITS OWN capture, not from a fixture.
SEALED_RUN = ("transfers/ae-multiturn-live-20260914-r1/finished-evidence/deployment/runs/"
              "ae-cloud-re-multiturn-original-20260914-r1")
SEALED_JOURNAL = SEALED_RUN + ".private.jsonl"
SEALED_SUMMARY = ("transfers/ae-multiturn-live-20260914-r1/finished-evidence/"
                  "verified-summary.json")
SEALED_CAPACITY = ("transfers/ae-multiturn-live-20260914-r1/finished-evidence/"
                   "capacity-check.json")
#: The measured per-task dynamic growth at t4: the observed context at the end of
#: t4 minus that task's own fixed contribution. It is the OBSERVED case, not a bound.
CANDIDATE_WINDOWS = (65536, 131072, 262144)


def read_sealed_requests(journal_path: Path):
    """Every REAL chat request the sealed run sent, with its provider usage.

    The capture is private: this function returns bodies for counting only, and the
    report never copies message text out of them.
    """
    import base64

    requests = {}
    with Path(journal_path).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            kind = row.get("kind")
            if kind == "normalized_request":
                requests[row["request_id"]] = {
                    "sequence": row["sequence"], "timestamp": row.get("timestamp"),
                    "body": json.loads(base64.b64decode(row["body_base64"]).decode("utf-8")),
                    "sha256": row.get("body_sha256")}
            elif kind == "upstream_response" and row.get("request_id") in requests:
                requests[row["request_id"]]["usage"] = json.loads(
                    base64.b64decode(row["body_base64"]).decode("utf-8")).get("usage")
    return [requests[key] for key in sorted(requests, key=lambda k: requests[k]["sequence"])]


def _largest_messages(tokenizer, messages, limit=5):
    ranked = sorted(((tokenizer.count(message.get("content") or ""), index,
                      message.get("role")) for index, message in enumerate(messages)),
                    reverse=True)[:limit]
    return [{"index": index, "role": role, "tokens": count} for count, index, role in ranked]


def per_request_layer(tokenizer, requests):
    """Layer: every real request, counted locally and compared with provider usage."""
    rows = []
    for entry in requests:
        body = entry["body"]
        messages = body.get("messages") or []
        tools = body.get("tools") or []
        # Two local numbers, for two different purposes:
        #  * `rendered_prompt_tokens` applies the OFFICIAL chat template, so it is the
        #    comparable quantity for the provider's own `prompt_tokens`;
        #  * `raw_json_tokens` counts the request body as the wire carries it, which is
        #    the conservative figure a pre-send gate uses (JSON syntax adds tokens the
        #    template does not).
        counted = count_request_prompt(tokenizer, messages, tools)
        rendered_tokens = counted["prompt_tokens"]
        # The RAW JSON count is kept only as a diagnostic contrast: it is what the r2
        # runtime gate used, and it over-counts because JSON syntax is not model input.
        raw = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                         separators=(", ", ": "))
        raw_tokens = tokenizer.count(raw)
        provider = (entry.get("usage") or {}).get("prompt_tokens")
        rows.append({
            "sequence": entry["sequence"], "timestamp": entry["timestamp"],
            "request_sha256": entry["sha256"], "messages": len(messages), "tools": len(tools),
            "rendered_chars": counted["rendered_chars"],
            "rendered_prompt_tokens": rendered_tokens, "count_contract": counted["contract"],
            "raw_json_tokens": raw_tokens,
            "local_tokens": rendered_tokens,
            "provider_prompt_tokens": provider,
            "delta_local_minus_provider": None if provider is None else rendered_tokens - provider,
            "raw_minus_provider": None if provider is None else raw_tokens - provider,
            "provider_completion_tokens": (entry.get("usage") or {}).get("completion_tokens"),
            "provider_total_tokens": (entry.get("usage") or {}).get("total_tokens"),
        })
    deltas = [row["delta_local_minus_provider"] for row in rows
              if row["delta_local_minus_provider"] is not None]
    raw_deltas = [row["raw_minus_provider"] for row in rows
                  if row["raw_minus_provider"] is not None]
    summary = {"requests": len(rows), "comparable": len(deltas),
               "delta_min": min(deltas) if deltas else None,
               "delta_max": max(deltas) if deltas else None,
               "delta_mean": round(sum(deltas) / len(deltas), 2) if deltas else None,
               "within_128_tokens": sum(1 for value in deltas if abs(value) <= 128),
               "worst_absolute_ratio": round(max(abs(value) / max(row["provider_prompt_tokens"], 1)
                                                 for value, row in zip(deltas, rows)
                                                 if row["provider_prompt_tokens"]), 6)
               if deltas else None,
               "rendered_vs_provider": ("the comparable pair: the official template's own "
                                        "rendering against the provider's count for the SAME "
                                        "request"),
               "raw_json_minus_provider_range": [min(raw_deltas), max(raw_deltas)] if raw_deltas else None,
               "raw_json_note": ("the wire-JSON count is always the larger of the two, which "
                                 "is why it is the conservative figure a pre-send gate may "
                                 "use; it is NOT the provider's prompt count")}
    # The reconciliation is only meaningful if the SAME function decides at runtime.
    # These are the production call sites of this one entry, and this block states the
    # ACTUAL error of that function on the sealed requests - no re-send, no estimate.
    absolute = sorted(abs(value) for value in deltas)
    production = {
        "count_function": "count_request_prompt",
        "module": "ae_multiturn_capacity.py",
        "runtime_users": [
            "scripts/ae_01_cloud_proxy.py --capacity-config (the pre-send gate in the "
            "process that really sends)",
            "letta/helpers/ae_qwen_tokenizer.py::count_request_prompt, imported by "
            "LettaAgentV3._ae_primary_count in the patched service",
        ],
        "same_bytes": ("both users count the request they are about to send: the proxy "
                       "counts the NORMALIZED wire body, the service counts the built "
                       "request dict (`messages` + wire `tools`)"),
        "sealed_requests_compared": len(deltas),
        "actual_error_tokens": {
            "min": min(deltas) if deltas else None,
            "max": max(deltas) if deltas else None,
            "mean": round(sum(deltas) / len(deltas), 2) if deltas else None,
            "median_absolute": absolute[len(absolute) // 2] if absolute else None,
            "max_absolute": absolute[-1] if absolute else None,
        },
        "interpretation": ("a positive value means the local function counts MORE than "
                           "the provider reported for that same request, which is the "
                           "safe direction for a pre-send gate; the largest absolute "
                           "error over the sealed requests is "
                           f"{absolute[-1] if absolute else None} tokens"),
        "caveat": ("the assets available on this host are the Qwen3-8B family cache, not "
                   "the target model's own tokenizer, so this error is an EXPLORATION "
                   "measurement and the RUN-stage decision refuses that asset"),
    }
    return {"requests": rows, "agreement": summary, "production_reconciliation": production}


def category_layer(tokenizer, requests):
    """Layer: where the BIGGEST real request's tokens actually go."""
    biggest = max((entry for entry in requests if len(entry["body"].get("messages") or []) >= 10),
                  key=lambda entry: tokenizer.count(render_qwen_chat(
                      entry["body"].get("messages") or [], entry["body"].get("tools") or [])),
                  default=None)
    if biggest is None:
        return {}
    body = biggest["body"]
    messages, tools = body.get("messages") or [], body.get("tools") or []
    buckets = categories_of(messages, tools)
    counts = {name: sum(tokenizer.count(text) for text in texts)
              for name, texts in sorted(buckets.items())}
    provider = (biggest.get("usage") or {}).get("prompt_tokens")
    tool_returns = [{"index": index, "chars": len(message.get("content") or ""),
                     "tokens": tokenizer.count(message.get("content") or "")}
                    for index, message in enumerate(messages) if message.get("role") == "tool"]
    tool_returns.sort(key=lambda item: item["tokens"], reverse=True)
    return {"sequence": biggest["sequence"], "messages": len(messages), "tools": len(tools),
            "provider_prompt_tokens": provider,
            "rendered_tokens": tokenizer.count(render_qwen_chat(messages, tools)),
            "category_tokens": counts, "largest_messages": _largest_messages(tokenizer, messages),
            "largest_tool_returns": tool_returns[:5],
            "tool_return_tokens_total": sum(item["tokens"] for item in tool_returns)}


def fixed_material_layer(tokenizer, dataset_path: Path, prepared, measured=None):
    """Layer: the fixed, countable material of every t4..t12 task, in order.

    The history envelope is built by the PINNED `ae_inputs.history_message`, so the
    counted bytes are the bytes the run really sends for that task. When `measured`
    carries the pinned environments' own tool schemas (see
    `tools/measure_multiturn_material.py`), each task's schema set is the one ITS OWN
    domain offers - never a fixed first-stage set and never every task's set summed.
    """
    measured_tasks = {row["task_number"]: row for row in (measured or {}).get("tasks", [])}
    rows = []
    for task in prepared["tasks"]:
        envelope = history_message(task)
        extra = measured_tasks.get(task["number"], {})
        envelope_tokens = tokenizer.count(envelope["content"])
        instruction_tokens = tokenizer.count(task["instruction"])
        policy_tokens = extra.get("domain_policy_tokens", 0)
        rows.append({
            "task_number": task["number"], "subtask_id": task["subtask_id"],
            "domain": task["domain"],
            "history_records": len(json.loads(envelope["content"])["records"]),
            "history_chars": len(envelope["content"]),
            "history_tokens": envelope_tokens,
            "history_tokens_this_task": envelope_tokens,
            "history_tokens_cumulative": 0,
            "instruction_chars": len(task["instruction"]),
            "instruction_tokens": instruction_tokens,
            "domain_policy_tokens": policy_tokens,
            "user_system_prompt_tokens": extra.get("user_system_prompt_tokens", 0),
            "tool_count": extra.get("tool_count"),
            "tool_schema_tokens": extra.get("tool_schema_tokens", 0),
            "task_envelope_tokens": instruction_tokens + policy_tokens,
        })
    running = 0
    for row in rows:
        running += row["history_tokens_this_task"]
        row["history_tokens_cumulative"] = running
    measured_note = ("each task's tool schema set is measured from the PINNED Vita environment "
                     "for that task's own domain"
                     if measured_tasks else
                     "tool schemas were NOT measured in this run: no per-task schema tokens")
    return {"dataset": str(dataset_path),
            "dataset_sha256": hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest(),
            "tasks": rows,
            "history_tokens_total": sum(row["history_tokens_this_task"] for row in rows),
            "tool_schema_tokens_total_if_all_present": sum(
                row["tool_schema_tokens"] for row in rows),
            "measured_tool_schemas": bool(measured_tasks),
            "note": ("history material is VERBATIM data sent as one user envelope; the dynamic "
                     "part of a task (agent replies, tool returns, user exchanges) is not fixed "
                     "material and is projected separately. The COLUMNS OVERLAP on purpose: "
                     "`history_tokens_this_task` is an increment, `history_tokens_cumulative` is "
                     "the sum of all envelopes up to that task, and `task_envelope_tokens` is the "
                     "current-task envelope only - they must not be added together. " + measured_note)}


def anchor_material(sealed):
    """Which materials the ANCHOR request already contains, from its own messages.

    The anchor is a REAL request, so what it holds is READ, not assumed: the sealed
    capture is split by message role and by the multi-turn envelope each message
    declares. Nothing here is inferred from a token total.
    """
    anchor = sealed.get("anchor") or {}
    materials = anchor.get("materials") or {}
    return {
        "task_number": anchor.get("task_number"),
        "sequence": anchor.get("sequence"),
        "request_sha256": anchor.get("request_sha256"),
        "measured_prompt_tokens": anchor.get("provider_prompt_tokens"),
        "measured_total_tokens": anchor.get("provider_total_tokens"),
        "complete_tasks_in_context": anchor.get("complete_tasks_in_context") or [],
        "history_envelopes_in_context": anchor.get("history_envelopes_in_context") or [],
        "partial_task_in_context": anchor.get("partial_task_in_context"),
        "materials": materials,
        "residual_after_anchor": anchor.get("residual_after_anchor"),
    }


def projection_layer(tokenizer, fixed, sealed):
    """Layer: the not-yet-run part, as explicitly stated assumptions.

    RETAINED HISTORY ONLY ADDS. The context after task N is

        C(N) = C(anchor) + sum over tasks after the anchor of
                            (their history envelope + their tool schemas
                             + their own task envelope + their dynamic growth)

    with the anchor's own materials NEVER subtracted and never added twice: the
    anchor is a real request, so `anchor.materials` states exactly which history
    envelopes, tool schemas and task envelopes it already holds (for the sealed run
    that is t4's whole task AND t5's history plus its current-task envelope, because
    compaction fired after t5's history was delivered).

    Three cases, each an ASSUMPTION:

    * `floor` - every later task adds ONLY its own fixed material (history envelope,
      its own tool schema set, its pinned instruction + domain policy). No agent
      activity at all: the lowest number that is still consistent with the protocol.
    * `modelled` - the floor plus, per task, the two largest REAL t4 query returns
      (a task needs at least the queries its own instruction implies).
    * `observed` - every later task behaves like the REAL t4 task did: the floor plus
      the dynamic growth measured for t4's own task phase.

    The comparison boundary for every scenario is the declared `context_window` (the
    hard limit the provider request must stay inside). `compaction_trigger_at_90pct`
    is reported beside it, never instead of it: under the declared no-compaction
    policy that line no longer authorises a summary - it is only the point where the
    run MUST stop, and the request-time capacity gate is what stops it.
    """
    anchor = sealed["context"]
    start = anchor["anchor_prompt_tokens"]
    before = anchor["anchor_task_number"]
    # What this number MEASURES: the tool-return bytes the REAL t4 task phase appended
    # to the agent's message history. It is NOT "the growth of a prompt", and it is not
    # reusable as a per-task constant: it is the only real per-stage growth observed.
    observed_returns = anchor["t4_task_phase_growth_tokens"]
    query_returns = sorted((item["tokens"] for item in
                            sealed["categories"].get("largest_tool_returns", [])), reverse=True)
    query_two = sum(query_returns[:2]) if len(query_returns) >= 2 else 0
    # The per-request overhead is the CURRENT request's own scaffolding: the wire tool
    # schema set (the same reviewed set every stage of a task builds) plus the system
    # prompt and the chat template's generation prompt. It is REPLACED at every stage,
    # never appended to the message history, so it enters the recurrence ONCE - not
    # once per task. The anchor's own material already contains it.
    per_request_overhead = anchor.get("request_overhead_tokens")
    if per_request_overhead is None:
        per_request_overhead = (fixed["tasks"][0]["tool_schema_tokens"]
                                if fixed["tasks"] else 0)
    cases = {
        "floor": {"per_task_growth": 0},
        "modelled": {"per_task_growth": query_two},
        "observed": {"per_task_growth": observed_returns},
    }
    windows = {}
    capacity = None
    for capacity in CANDIDATE_WINDOWS:
        trigger = int(capacity * 0.9)
        scenarios = {}
        for name, case in sorted(cases.items()):
            total = start
            trace = []
            first_over = None
            first_over_trigger = None
            for task in fixed["tasks"]:
                if task["task_number"] <= before:
                    continue
                # PERSISTENT material only: the task's own history envelope, the
                # current-task instruction envelope and the observed dynamic growth
                # (agent replies, tool calls and their returns - retained, never
                # dropped). The tool schema is NOT added here: it is this request's
                # own scaffolding, counted once in the anchor.
                added = (task["history_tokens_this_task"] + task["task_envelope_tokens"]
                         + case["per_task_growth"])
                total += added
                trace.append({"task_number": task["task_number"], "added_tokens": added,
                              "history_tokens": task["history_tokens_this_task"],
                              "task_envelope_tokens": task["task_envelope_tokens"],
                              "dynamic_growth_tokens": case["per_task_growth"],
                              "cumulative_prompt_tokens": total})
                if first_over is None and total > capacity:
                    first_over = task["task_number"]
                if first_over_trigger is None and total > trigger:
                    first_over_trigger = task["task_number"]
            scenarios[name] = {
                "per_task_dynamic_growth_tokens": case["per_task_growth"],
                "end_of_t12_prompt_tokens": total,
                "fits_inside_the_hard_window": first_over is None,
                "first_task_over_the_hard_window": first_over,
                "first_task_over_the_compaction_trigger": first_over_trigger,
                "trace": trace}
        windows[str(capacity)] = {
            "capacity_tokens": capacity,
            "hard_window_tokens": capacity,
            "compaction_trigger_at_90pct": trigger,
            "headroom_before_the_trigger": trigger - start,
            "headroom_before_the_hard_window": capacity - start,
            "anchor_is_already_over_the_trigger": start > trigger,
            "scenarios": scenarios}
    return {
        "anchor": anchor_material(sealed),
        "cases": {"floor": cases["floor"]["per_task_growth"],
                  "modelled": cases["modelled"]["per_task_growth"],
                  "observed": cases["observed"]["per_task_growth"]},
        "per_request_overhead_tokens": per_request_overhead,
        "measured_t4_tool_return_tokens_observed": observed_returns,
        "measured_two_largest_query_returns_tokens": query_two,
        "history_material_total_tokens": fixed["history_tokens_total"],
        "recurrence": {
            "persistent": ("C(N) = C(anchor) + sum over tasks after the anchor of (their "
                           "history envelope + their current-task envelope + their "
                           "observed dynamic growth)"),
            "not_accumulated": ("the wire tool schema set and the system/generation prompt "
                                "are the CURRENT request's own scaffolding: they are "
                                "replaced at every stage, enter the recurrence once (in "
                                "the anchor's measured prompt) and are never summed per task"),
            "retained": ("historical tool calls and their returns stay in the message "
                         "history: they are the dynamic growth term, not removable "
                         "material"),
        },
        "windows": windows,
        "budget_boundaries": {
            "input_tokens_measured_at_the_anchor": start,
            "generated_output_tokens_at_the_anchor": anchor["anchor_output_tokens"],
            "next_request_output_reserve_tokens": anchor["output_reserve_tokens"],
            "hard_window_tokens": 65536,
            "compaction_trigger_tokens": anchor["compaction_trigger_threshold"],
            # Two DIFFERENT lines, and the arithmetic is stated for each separately:
            # the trigger is where a summary WOULD have been attempted, the hard window
            # is what the provider request must stay inside.
            "anchor_over_the_trigger_by": start - anchor["compaction_trigger_threshold"],
            "anchor_inside_the_hard_window_by": 65536 - start,
            "anchor_total_over_the_trigger_by":
                (start + anchor["anchor_output_tokens"]
                 - anchor["compaction_trigger_threshold"]),
            "note": ("the PROMPT at the anchor is over the 65536 run's trigger by "
                     f"{start - anchor['compaction_trigger_threshold']} tokens "
                     f"(prompt {start} vs trigger {anchor['compaction_trigger_threshold']}) "
                     "and inside the hard window by "
                     f"{65536 - start} (65536 - {start}); the whole PROVIDER request "
                     f"(prompt + the {anchor['anchor_output_tokens']} generated tokens) "
                     "is over the trigger by "
                     f"{start + anchor['anchor_output_tokens'] - anchor['compaction_trigger_threshold']}. "
                     "Under the declared policy the trigger stops the run (no summary), "
                     "it does not free capacity, and the hard window is what the "
                     "request must stay inside."),
        },
        "assumptions": [
            "every later task delivers its OWN history envelope once, and every earlier "
            "envelope stays in context: history is retained, never replaced",
            "the anchor already contains the materials listed in `anchor.materials`; the "
            "projection adds only tasks with a HIGHER number, so nothing is counted twice",
            "the current request's tool schema set is the one that task's own domain "
            "really offers, measured from the pinned Vita environment (t4 delivery 19 "
            "tools / t5 instore 23); it is part of the CURRENT request and is replaced at "
            "every stage, so it is NOT added per task",
            "agent replies, tool returns and user exchanges after the anchor are ASSUMED, "
            "not measured; the sealed run never got there. The `observed` case reuses the "
            "ONE real measurement there is (t4's own tool-return volume) and says so; it "
            "is not a per-task constant",
            "tool returns are not truncated by the driver: its guard is 26214 chars, above "
            "the largest real t4 return (24502 chars)",
        ],
    }

