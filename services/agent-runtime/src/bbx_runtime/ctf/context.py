"""Deterministic, model-visible context estimates and durable result previews."""

import hashlib
import json
from collections.abc import Sequence
from copy import copy, deepcopy
from typing import Any

from agent_framework import GROUP_ANNOTATION_KEY, Content, Message, annotate_message_groups
from bbx_contracts.models import ModelConfig

from bbx_runtime.ctf.budget import ctf_max_output_tokens


def message_signature(message: Message) -> str:
    """Compare saved material without serializing trace-only envelope metadata."""
    payload = {
        "role": str(message.role),
        "message_id": message.message_id,
        "author_name": message.author_name,
        "contents": [content.to_dict() for content in message.contents],
        "group": message.additional_properties.get(GROUP_ANNOTATION_KEY),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def estimate_text_tokens(text: str) -> int:
    """Estimate without provider calls; UTF-8 non-ASCII and punctuation stay conservative.

    This is a sizing heuristic, not the provider's tokenizer. The input budget also
    leaves a 20% margin, and reported provider usage remains authoritative.
    """
    ascii_words = sum(char.isascii() and (char.isalnum() or char.isspace()) for char in text)
    other_bytes = sum(
        len(char.encode("utf-8"))
        for char in text
        if not (char.isascii() and (char.isalnum() or char.isspace()))
    )
    return (ascii_words + 2) // 3 + other_bytes


def clear_protected_text(value: str | None) -> str:
    """Match MAF basis 2: tokenise clear JSON reasoning, never opaque signatures."""
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except ValueError:
        return ""
    if not isinstance(parsed, (dict, list)):
        return ""

    def strip(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: strip(value) for key, value in item.items() if key != "encrypted_content"}
        if isinstance(item, list):
            return [strip(value) for value in item]
        return item

    return json.dumps(strip(parsed), ensure_ascii=False)


def visible_message_text(
    message: Message, *, provider: str | None = None, reasoning_ids: set[str] | None = None
) -> str:
    """Exclude Session IDs, attribution and trace-only reasoning metadata."""
    parts = [str(message.role), message.author_name or ""]
    for content in message.contents:
        if content.type == "text":
            parts.append(content.text or "")
        elif content.type == "text_reasoning":
            encrypted = content.protected_data or content.additional_properties.get(
                "encrypted_content"
            )
            if provider in {"openai_responses", "azure_openai_responses"}:
                # Stateless Responses forwards only replayable encrypted reasoning;
                # a plain reasoning trace does not become request input.
                if content.id and (encrypted or content.id in (reasoning_ids or set())):
                    parts.append(content.text or "")
                    reasoning_text = content.additional_properties.get("reasoning_text")
                    if isinstance(reasoning_text, str):
                        parts.append(reasoning_text)
            else:
                parts.append(content.text or "")
                parts.append(clear_protected_text(content.protected_data))
        elif content.type == "function_call":
            arguments = content.arguments
            parts.extend(
                [
                    content.name or "",
                    content.call_id or "",
                    json.dumps(arguments, ensure_ascii=False),
                ]
            )
        elif content.type == "function_result":
            parts.extend([content.call_id or "", str(content.result)])
            if content.items:
                parts[-1] = "\n".join(
                    item.text or "" for item in content.items if item.type == "text"
                )
    return "\n".join(parts)


def estimate_message_lengths(
    messages: Sequence[Message], *, provider: str | None = None
) -> list[int]:
    # Responses groups summaries across messages by reasoning id and forwards them
    # only when that group has a replayable encrypted payload.
    reasoning_ids = {
        content.id
        for message in messages
        for content in message.contents
        if content.type == "text_reasoning"
        and content.id
        and (content.protected_data or content.additional_properties.get("encrypted_content"))
    }
    # Rich content needs an explicit reserve; do not count serialized image bytes as text.
    return [
        16
        + estimate_text_tokens(
            visible_message_text(message, provider=provider, reasoning_ids=reasoning_ids)
        )
        + sum(4096 for content in message.contents if content.type in {"data", "uri"})
        for message in messages
    ]


def estimate_messages_tokens(messages: Sequence[Message], *, provider: str | None = None) -> int:
    return sum(estimate_message_lengths(messages, provider=provider))


def input_token_limit(model: ModelConfig, threshold: int) -> int:
    """Reserve output capacity, then leave 20% input headroom; this is a hard gate."""
    available = threshold
    if model.context_window is not None:
        available = min(available, max(0, model.context_window - ctf_max_output_tokens(model)))
    return max(0, int(available * 0.8))


def fixed_prompt_tokens(instructions: str, tools: Sequence[Any]) -> int:
    definitions = [
        {"name": tool.name, "description": tool.description, "parameters": tool.parameters()}
        for tool in tools
    ]
    return (
        16
        + estimate_text_tokens(instructions)
        + estimate_text_tokens(json.dumps(definitions, ensure_ascii=False))
    )


def stable_tool_previews(
    messages: Sequence[Message], state: dict[str, Any], limit_tokens: int
) -> list[Message]:
    """Choose each result's model view once, including unchanged short results.

    Persist the decision before dispatch with the usual checkpoint. New results
    cannot shorten a previous result, including after a process restart.
    """
    previews = state.setdefault("ctf_tool_previews", {})
    view = list(messages)
    allowance = max(64, min(8192, limit_tokens // 8))
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        for content_index, content in enumerate(message.contents):
            if content.type != "function_result":
                continue
            key = f"{message.message_id}:{content.call_id}:{content_index}"
            if key not in previews:
                result = str(content.result)
                if estimate_text_tokens(result) <= allowance:
                    previews[key] = None
                else:
                    # A byte-bounded head/tail also bounds punctuation-heavy output.
                    half = max(16, (allowance - 128) // 2)
                    raw = result.encode("utf-8")
                    previews[key] = (
                        f"[Truncated model preview: message_id={message.message_id}; "
                        f"call_id={content.call_id}; full result remains in the saved Session; "
                        f"original_characters={len(result)}]\n"
                        + raw[:half].decode("utf-8", errors="ignore")
                        + "\n[... omitted from model view ...]\n"
                        + raw[-half:].decode("utf-8", errors="ignore")
                    )
            preview = previews[key]
            if preview is not None:
                if view[index] is message:
                    view[index] = deepcopy(message)
                copied = view[index].contents[content_index]
                copied.result = preview
                copied.items = Content.from_function_result(
                    content.call_id or "", result=preview
                ).items
    return view


def _history_boundaries(messages: Sequence[Message]) -> tuple[list[int], set[int], int]:
    """Use MAF's native atomic groups, with CTF admission/completion safeguards."""
    grouped = []
    original_spans: dict[str, tuple[int, int]] = {}

    def extend_span(identity: str, index: int) -> None:
        first = original_spans.get(identity, (index, index))[0]
        original_spans[identity] = (first, index)

    for index, message in enumerate(messages):
        # Native annotation assigns missing IDs and edits properties. Copy just
        # those mutable envelopes; Content and original Session stay untouched.
        shadow = copy(message)
        shadow.additional_properties = dict(message.additional_properties)
        annotation = shadow.additional_properties.get(GROUP_ANNOTATION_KEY)
        if isinstance(annotation, dict):
            shadow.additional_properties[GROUP_ANNOTATION_KEY] = dict(annotation)
            if isinstance(annotation.get("id"), str):
                extend_span(f"existing:{annotation['id']}", index)
        for content in message.contents:
            if content.type == "text_reasoning" and content.id:
                extend_span(f"reasoning:{content.id}", index)
        grouped.append(shadow)

    annotate_message_groups(grouped)
    native_groups: dict[str, list[int]] = {}
    group_kinds: dict[str, str] = {}
    for index, message in enumerate(grouped):
        annotation = message.additional_properties[GROUP_ANNOTATION_KEY]
        group_id = annotation["id"]
        native_groups.setdefault(group_id, []).append(index)
        group_kinds[group_id] = annotation["kind"]
        extend_span(f"native:{group_id}", index)

    forbidden = {
        boundary
        for first, last in original_spans.values()
        for boundary in range(first + 1, last + 1)
    }
    unfinished_from = len(messages)
    candidates = []
    for group_id, indices in native_groups.items():
        # Native groups establish pairing, including non-contiguous late results.
        # They intentionally lack a completed flag: CTF cannot evict a group
        # while any declaration still awaits its result.
        declared = {
            content.call_id
            for index in indices
            for content in messages[index].contents
            if content.type == "function_call"
            and content.call_id
            and not content.informational_only
        }
        returned = {
            content.call_id
            for index in indices
            for content in messages[index].contents
            if content.type == "function_result" and content.call_id
        }
        if declared - returned:
            unfinished_from = min(unfinished_from, indices[0])
        if group_kinds[group_id] in {"assistant_text", "tool_call"}:
            candidates.append(indices[-1] + 1)
    completed = sorted(
        boundary
        for boundary in candidates
        if boundary <= unfinished_from and boundary not in forbidden
    )
    return completed, forbidden, unfinished_from


def safe_history_boundaries(messages: Sequence[Message], start: int) -> list[int]:
    completed, _, _ = _history_boundaries(messages)
    return [boundary for boundary in completed if boundary > start]


def align_history_start(messages: Sequence[Message], start: int) -> int | None:
    """Move legacy cutoffs forward only; never reintroduce an omitted tool call."""
    if start == 0:
        return 0
    completed, forbidden, unfinished_from = _history_boundaries(messages)
    if start > unfinished_from:
        return None
    if start not in forbidden:
        return start
    return next((boundary for boundary in completed if boundary > start), None)
