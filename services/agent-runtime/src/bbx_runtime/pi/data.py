"""Platform conversation envelopes and the lossless Pi transcript boundary.

The database envelope predates Pi. Keeping it readable avoids rewriting historical
tasks; all new assistant blocks also retain their native Pi representation.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

UsageDetails = dict[str, Any]
GROUP_ANNOTATION_KEY = "_group"


class Content(BaseModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    type: str
    text: str | None = None
    protected_data: str | None = None
    uri: str | None = None
    media_type: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | Mapping[str, Any] | None = None
    result: Any = None
    items: list[Content] | None = None
    exception: str | None = None
    informational_only: bool = False
    usage_details: UsageDetails | None = None
    id: str | None = None
    file_id: str | None = None
    additional_properties: dict[str, Any] = Field(default_factory=dict)
    raw_representation: Any = Field(default=None, exclude=True)

    def __init__(self, type: str, **kwargs: Any) -> None:
        super().__init__(type=type, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        result = self.model_dump(exclude_none=True, exclude_defaults=True)
        result["additional_properties"] = deepcopy(self.additional_properties)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Content:
        return cls.model_validate(value)

    @classmethod
    def from_text(cls, text: str, **kwargs: Any) -> Content:
        return cls("text", text=text, **kwargs)

    @classmethod
    def from_text_reasoning(cls, text: str | None = None, **kwargs: Any) -> Content:
        return cls("text_reasoning", text=text, **kwargs)

    @classmethod
    def from_function_call(
        cls, call_id: str, name: str, *, arguments: Any = None, **kwargs: Any
    ) -> Content:
        return cls("function_call", call_id=call_id, name=name, arguments=arguments, **kwargs)

    @classmethod
    def from_function_result(cls, call_id: str, *, result: Any = None, **kwargs: Any) -> Content:
        if "items" not in kwargs:
            kwargs["items"] = result_contents(result)
        if result is None:
            result = ""
        elif isinstance(result, Content):
            result = result.text or ""
        elif isinstance(result, (list, tuple)) and all(
            isinstance(item, Content) for item in result
        ):
            result = "\n".join(item.text or "" for item in result if item.type == "text")
        elif not isinstance(result, str):
            result = json.dumps(
                result.to_dict() if isinstance(result, Content) else result, default=str
            )
        return cls("function_result", call_id=call_id, result=result, **kwargs)

    @classmethod
    def from_usage(cls, usage_details: UsageDetails) -> Content:
        return cls("usage", usage_details=usage_details)

    @classmethod
    def from_data(cls, data: bytes | str, media_type: str, **kwargs: Any) -> Content:
        encoded = base64.b64encode(data).decode() if isinstance(data, bytes) else data
        return cls(
            "data", uri=f"data:{media_type};base64,{encoded}", media_type=media_type, **kwargs
        )


def result_contents(value: Any) -> list[Content]:
    if value is None:
        return [Content.from_text("")]
    if isinstance(value, Content):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(item, Content) for item in value):
        return list(value)
    return [Content.from_text(value if isinstance(value, str) else json.dumps(value, default=str))]


class Message(BaseModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    role: str
    contents: list[Content] = Field(default_factory=list)
    message_id: str | None = None
    author_name: str | None = None
    additional_properties: dict[str, Any] = Field(default_factory=dict)
    raw_representation: Any = Field(default=None, exclude=True)

    def __init__(
        self, role: str, contents: Sequence[Any] | str | None = None, **kwargs: Any
    ) -> None:
        if isinstance(contents, str):
            contents = [contents]
        values = [
            Content.from_text(item) if isinstance(item, str) else item for item in contents or []
        ]
        super().__init__(role=role, contents=values, **kwargs)

    @property
    def text(self) -> str:
        return "".join(item.text or "" for item in self.contents if item.type == "text")

    def to_dict(self) -> dict[str, Any]:
        result = self.model_dump(exclude_none=True, exclude_defaults=True)
        result["type"] = "message"
        result["contents"] = [item.to_dict() for item in self.contents]
        result["additional_properties"] = deepcopy(self.additional_properties)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Message:
        return cls.model_validate(value)


def _encode(value: Any) -> Any:
    if isinstance(value, (Message, Content)):
        return value.to_dict()
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("type") == "message" and "role" in value and "contents" in value:
            return Message.from_dict(value)
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


class AgentSession:
    """A platform CAS envelope, not an execution engine or a second session store."""

    def __init__(self, *, session_id: str | None = None, service_session_id: Any = None) -> None:
        self.session_id = session_id or str(uuid4())
        self.service_session_id = service_session_id
        self.state: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "session",
            "session_id": self.session_id,
            "service_session_id": self.service_session_id,
            "state": _encode(self.state),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentSession:
        session = cls(
            session_id=value.get("session_id"), service_session_id=value.get("service_session_id")
        )
        session.state = _decode(deepcopy(value.get("state", {})))
        history = session.state.get("in_memory", {})
        if isinstance(history, dict) and "messages" in history:
            history["messages"] = [
                Message.from_dict(item) if isinstance(item, dict) else item
                for item in history["messages"]
            ]
        return session


class ChatResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")
    messages: list[Message] = Field(default_factory=list)
    response_id: str | None = None
    model: str | None = None
    usage_details: UsageDetails | None = None
    finish_reason: str | None = None
    raw_representation: Any = None

    @property
    def text(self) -> str:
        return "".join(message.text for message in self.messages)


class ChatResponseUpdate(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")
    role: str | None = "assistant"
    contents: list[Content] = Field(default_factory=list)
    response_id: str | None = None
    message_id: str | None = None
    model_id: str | None = None
    finish_reason: str | None = None
    raw_representation: Any = None

    @property
    def text(self) -> str:
        return "".join(content.text or "" for content in self.contents if content.type == "text")


def annotate_message_groups(messages: Sequence[Message], **_kwargs: Any) -> None:
    """Keep persisted atomic spans and Pi reasoning/tool pairs inseparable."""
    parents = list(range(len(messages)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(left, right):
        left, right = root(left), root(right)
        parents[max(left, right)] = min(left, right)

    existing, calls = {}, {}
    reasoning = []
    for index, message in enumerate(messages):
        if not message.message_id:
            message.message_id = f"msg_{index}"
        annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY, {})
        group_id = annotation.get("id") if isinstance(annotation, dict) else None
        if group_id:
            if group_id in existing:
                join(index, existing[group_id])
            existing[group_id] = index
        declarations = [
            c.call_id
            for c in message.contents
            if c.type == "function_call" and not c.informational_only and c.call_id
        ]
        if declarations:
            for previous in reasoning:
                join(index, previous)
            for call_id in declarations:
                calls[call_id] = index
            reasoning = []
        elif (
            message.role == "assistant"
            and message.contents
            and all(c.type == "text_reasoning" for c in message.contents)
        ):
            reasoning.append(index)
        else:
            reasoning = []
        for content in message.contents:
            if content.type == "function_result" and content.call_id in calls:
                join(index, calls[content.call_id])
    grouped = {}
    for index in range(len(messages)):
        grouped.setdefault(root(index), []).append(index)
    for ordinal, indices in enumerate(grouped.values()):
        members = [messages[index] for index in indices]
        original = next(
            (
                m.additional_properties[GROUP_ANNOTATION_KEY]
                for m in members
                if isinstance(m.additional_properties.get(GROUP_ANNOTATION_KEY), dict)
            ),
            {},
        )
        kind = (
            "tool_call"
            if any(
                c.type in {"function_call", "function_result"} and not c.informational_only
                for m in members
                for c in m.contents
            )
            else (
                "assistant_text" if any(m.role == "assistant" for m in members) else members[0].role
            )
        )
        annotation = {
            "id": original.get("id", f"group_{members[0].message_id}"),
            "kind": original.get("kind", kind),
            "index": original.get("index", ordinal),
            "has_reasoning": any(c.type == "text_reasoning" for m in members for c in m.contents),
            "token_count": original.get("token_count"),
            "token_count_basis": original.get("token_count_basis"),
        }
        for message in members:
            message.additional_properties[GROUP_ANNOTATION_KEY] = dict(annotation)


def pi_content(content: Content, *, api: str | None = None) -> dict[str, Any] | None:
    native = deepcopy(content.additional_properties.get("pi_content", {}))
    if content.type == "text":
        return {**native, "type": "text", "text": content.text or ""}
    if content.type == "text_reasoning":
        signature = content.protected_data
        if signature and not native and api in {"openai-responses", "azure-openai-responses"}:
            # MAF stored the encrypted value and item ID in separate fields;
            # Pi stores the complete provider item as its opaque signature.
            signature = json.dumps(
                {
                    "type": "reasoning",
                    "encrypted_content": signature,
                    "summary": [{"type": "summary_text", "text": content.text}]
                    if content.text
                    else [],
                    **({"id": content.id} if content.id else {}),
                }
            )
        return {
            **native,
            "type": "thinking",
            "thinking": content.text or "",
            **({"thinkingSignature": signature} if signature else {}),
        }
    if content.type == "function_call" and not content.informational_only:
        arguments = content.arguments or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return {
            **native,
            "type": "toolCall",
            "id": content.call_id,
            "name": content.name,
            "arguments": arguments,
        }
    if content.type in {"data", "uri"} and content.uri and content.uri.startswith("data:"):
        mime, encoded = content.uri.split(",", 1)
        return {"type": "image", "mimeType": mime[5:].split(";", 1)[0], "data": encoded}
    return None


def pi_messages(
    messages: Sequence[Message],
    *,
    api: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    result = []
    names = {c.call_id: c.name for m in messages for c in m.contents if c.type == "function_call"}
    for message in messages:
        native = deepcopy(message.additional_properties.get("pi_message", {}))
        timestamp = native.get("timestamp", 0)
        if message.role == "tool":
            from bbx_runtime.responses_history import _result_items

            for item in message.contents:
                if item.type != "function_result":
                    continue
                blocks = [pi_content(c, api=api) for c in _result_items(item)]
                result.append(
                    {
                        **native,
                        "role": "toolResult",
                        "toolCallId": item.call_id,
                        "toolName": names.get(item.call_id) or "tool",
                        "content": [c for c in blocks if c is not None],
                        "isError": bool(
                            item.exception
                            or any(
                                c.additional_properties.get("ctf_tool_error")
                                or c.additional_properties.get("bbx_tool_error")
                                for c in item.items or []
                            )
                        ),
                        "timestamp": timestamp,
                    }
                )
            continue
        blocks = [pi_content(content, api=api) for content in message.contents]
        if message.role == "system":
            result.append({"role": "system", "content": message.text, "timestamp": timestamp})
        else:
            if message.role == "assistant":
                native = {
                    "api": api or "openai-completions",
                    "provider": provider or "legacy",
                    "model": model or "legacy",
                    "stopReason": "toolUse"
                    if any(c.type == "function_call" for c in message.contents)
                    else "stop",
                    "usage": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 0,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "total": 0,
                        },
                    },
                    **native,
                }
            result.append(
                {
                    **native,
                    "role": message.role,
                    "content": [block for block in blocks if block is not None],
                    "timestamp": timestamp,
                    "bbxMessageId": message.message_id,
                }
            )
    return result


def from_pi_message(value: dict[str, Any]) -> Message:
    contents: list[Content] = []
    role = value["role"]
    if role == "toolResult":
        detail = value.get("details")
        if isinstance(detail, dict) and isinstance(detail.get("bbx_result_content"), dict):
            return Message(
                "tool",
                contents=[Content.from_dict(detail["bbx_result_content"])],
                message_id=str(uuid4()),
            )
        saved = (
            value.get("details", {}).get("bbx_result")
            if isinstance(value.get("details"), dict)
            else None
        )
        result: Any = (
            _decode(saved)
            if saved is not None
            else "\n".join(
                block.get("text", "") for block in value["content"] if block["type"] == "text"
            )
        )
        if isinstance(result, list):
            result = [
                Content.from_dict(item) if isinstance(item, dict) and "type" in item else item
                for item in result
            ]
        contents = [Content.from_function_result(value["toolCallId"], result=result)]
        if value.get("isError"):
            contents[0].exception = "ToolError"
        role = "tool"
    else:
        blocks = value.get("content", [])
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        for block in blocks:
            metadata = {
                "pi_content": {
                    key: deepcopy(value)
                    for key, value in block.items()
                    if key
                    not in {"text", "thinking", "arguments", "id", "name", "data", "mimeType"}
                }
            }
            if block["type"] == "text":
                contents.append(Content.from_text(block["text"], additional_properties=metadata))
            elif block["type"] == "thinking":
                signature = block.get("thinkingSignature")
                protected = signature
                identity = None
                if signature and value.get("api") in {"openai-responses", "azure-openai-responses"}:
                    item = json.loads(signature)
                    identity = item.get("id")
                    if not item.get("encrypted_content"):
                        protected = None
                    else:
                        metadata["pi_content"].pop("thinkingSignature", None)
                contents.append(
                    Content.from_text_reasoning(
                        block.get("thinking", ""),
                        protected_data=protected,
                        id=identity,
                        additional_properties=metadata,
                    )
                )
            elif block["type"] == "toolCall":
                contents.append(
                    Content.from_function_call(
                        block["id"],
                        block["name"],
                        arguments=block["arguments"],
                        additional_properties=metadata,
                    )
                )
            elif block["type"] == "image":
                contents.append(
                    Content.from_data(
                        block["data"],
                        block["mimeType"],
                        additional_properties=metadata,
                    )
                )
    envelope = {
        key: deepcopy(value[key])
        for key in (
            "api",
            "provider",
            "model",
            "usage",
            "stopReason",
            "timestamp",
            "thinkingLevel",
            "providerThinkingLevel",
            "responseId",
        )
        if key in value
    }
    return Message(
        role,
        contents=contents,
        message_id=value.get("bbxMessageId") or str(uuid4()),
        additional_properties={"pi_message": envelope},
    )
