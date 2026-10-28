"""Request-only observations for completed, plaintext Responses tool groups."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from agent_framework import GROUP_ANNOTATION_KEY, Content, Message, annotate_message_groups

logger = logging.getLogger(__name__)
_VISIBLE_TYPES = {"text", "data", "uri", "hosted_file"}
_SHELL_KEYS = {
    "openai.responses.shell.output_type",
    "openai.responses.local_shell.call_item_id",
}


def _result_items(content: Content) -> list[Content]:
    """Match MAF's current text result while retaining native rich result items."""
    current = list(
        Content.from_function_result(content.call_id or "", result=content.result).items or []
    )
    items = content.items or []
    if not any(item.type in {"data", "uri", "hosted_file"} for item in items):
        return current
    previous_text = "\n".join(item.text for item in items if item.type == "text" and item.text)
    current_text = "\n".join(item.text for item in current if item.type == "text" and item.text)
    if current_text.startswith(previous_text):
        suffix = current_text[len(previous_text) :]
        return [*items, *([Content.from_text(suffix)] if suffix else [])]
    # A replacement text result supersedes old text, while images/files remain typed.
    return [*current, *(item for item in items if item.type != "text")]


def project_tool_observations(messages: Sequence[Message]) -> Sequence[Message]:
    """Replace whole replay-ineligible groups without changing the durable transcript.

    MAF remains responsible for grouping and validating everything not projected.
    Incomplete, ambiguous and hosted-tool groups deliberately retain its rejection.
    """
    if not any(
        content.type == "text_reasoning"
        and content.id
        and not (content.protected_data or content.additional_properties.get("encrypted_content"))
        for message in messages
        for content in message.contents
    ):
        return messages
    shadow = deepcopy(list(messages))
    annotate_message_groups(shadow)
    groups: dict[str, list[int]] = {}
    for index, message in enumerate(shadow):
        group_id = message.additional_properties[GROUP_ANNOTATION_KEY]["id"]
        groups.setdefault(group_id, []).append(index)
    replacements: dict[int, list[Message]] = {}
    projected_groups = 0
    for group_id, indices in groups.items():
        contents = [content for index in indices for content in shadow[index].contents]
        reasoning = [content for content in contents if content.type == "text_reasoning"]
        replayable_ids = {
            content.id
            for content in reasoning
            if content.id
            and (content.protected_data or content.additional_properties.get("encrypted_content"))
        }
        missing = [content for content in reasoning if content.id not in replayable_ids]
        # MAF emits empty reasoning markers, and gateways can emit whitespace-only
        # deltas. Complete tool evidence does not depend on visible reasoning text.
        if replayable_ids or not missing or any(not content.id for content in missing):
            continue
        calls = [content for content in contents if content.type == "function_call"]
        results = [content for content in contents if content.type == "function_result"]
        call_counts = Counter(content.call_id for content in calls)
        result_counts = Counter(content.call_id for content in results)
        if (
            not calls
            or call_counts != result_counts
            or any(not call_id or count != 1 for call_id, count in call_counts.items())
            or any(
                content.type
                not in _VISIBLE_TYPES | {"text_reasoning", "function_call", "function_result"}
                or content.informational_only
                or _SHELL_KEYS.intersection(content.additional_properties)
                for content in contents
            )
            or any(
                item.type not in _VISIBLE_TYPES
                for content in results
                for item in content.items or []
            )
            or any(shadow[index].role not in {"assistant", "tool"} for index in indices)
        ):
            continue
        names = {content.call_id: content.name for content in calls}
        if any(not name for name in names.values()):
            continue
        for index in indices:
            original = shadow[index]
            provenance = {
                "bbx_responses_observation": {
                    "source_group_id": group_id,
                    "source_message_id": original.message_id,
                }
            }
            projected: list[Message] = []
            for position, content in enumerate(original.contents):
                if content.type == "text_reasoning":
                    continue
                if content.type in _VISIBLE_TYPES:
                    projected.append(
                        Message(
                            role=original.role if original.role == "assistant" else "user",
                            contents=[content],
                            additional_properties=deepcopy(provenance),
                        )
                    )
                    continue
                metadata: dict[str, Any] = {
                    "source_group_id": group_id,
                    "source_message_id": original.message_id,
                    "call_id": content.call_id,
                    "name": names[content.call_id],
                }
                if content.type == "function_call":
                    metadata["arguments"] = content.arguments
                label = (
                    "Completed tool call (historical observation; do not execute): "
                    if content.type == "function_call"
                    else "Completed tool result (historical observation): "
                )
                observation = [Content.from_text(label + json.dumps(metadata, ensure_ascii=False))]
                if content.type == "function_result":
                    # Canonical MAF result items retain native images/files instead of base64 text.
                    observation.extend(_result_items(content))
                projected.append(
                    Message(
                        role="user",
                        contents=observation,
                        message_id=f"bbx-observation:{original.message_id}:{position}",
                        additional_properties=deepcopy(provenance),
                    )
                )
            replacements[index] = projected
        projected_groups += 1
    if not replacements:
        return messages
    logger.info(
        "Responses request projected completed tool groups: groups=%d messages=%d",
        min(projected_groups, 1_000_000),
        min(len(replacements), 1_000_000),
    )
    return [
        projected
        for index, message in enumerate(messages)
        for projected in replacements.get(index, [message])
    ]
