"""Bounded reads of settled results, a member's tools and deferred metadata."""

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.exc import NoResultFound

from bbx_blackboard.store import schema as s
from bbx_blackboard.store.repository import row


def _page(text: str, offset: int, limit: int) -> dict[str, Any]:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
        raise HTTPException(422, "Invalid result page")
    end = min(len(text), offset + limit)
    return {
        "result": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "total_characters": len(text),
    }


class CtfReadbackMixin:
    repo: Any
    _task: Any
    _fence: Any
    turn_result: Any

    async def _readback_reader(self, conn, tid, actor):
        try:
            await self._task(conn, tid)
            member = await row(conn, s.ctf_members, tid, actor)
        except (KeyError, NoResultFound):
            raise HTTPException(404, "CTF reader not found") from None
        if member["lifecycle"] == "removed":
            _, turn = await self._fence(conn, tid, actor, member["generation"])
            if turn["purpose"] != "review":
                raise HTTPException(409, "Removed member has no reader authority")

    async def read_turn_result(self, tid, actor, turn_id, offset=0, limit=2000):
        _page("", offset, limit)
        async with self.repo.engine.begin() as conn:
            if actor == "user":
                try:
                    await self._task(conn, tid)
                except KeyError:
                    raise HTTPException(404, "CTF task not found") from None
            else:
                await self._readback_reader(conn, tid, actor)
        try:
            result = await self.turn_result(tid, turn_id, actor)
        except NoResultFound:
            raise HTTPException(404, "Turn result not found") from None
        page = _page(result.get("final_answer") or "", offset, limit)
        answer = page.pop("result")
        return {**result, **page, "final_answer": answer}

    async def read_saved_tool_result(self, tid, actor, call_id, offset=0, limit=2000):
        _page("", offset, limit)
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 200:
            raise HTTPException(422, "Invalid tool call ID")
        async with self.repo.engine.begin() as conn:
            await self._readback_reader(conn, tid, actor)
            session = (
                (
                    await conn.execute(
                        select(
                            func.jsonb_path_query_array(
                                s.agent_sessions.c.session,
                                cast(
                                    '$.state.in_memory.messages[*] ? (@.type() == "object" '
                                    '&& @.contents.type() == "array").contents[*] ? '
                                    '(@.type() == "object" && @.type == "function_result" '
                                    "&& @.call_id == $call_id)",
                                    JSONPATH,
                                ),
                                func.jsonb_build_object("call_id", call_id),
                                type_=JSONB,
                            ).label("contents"),
                            s.agent_sessions.c.session["state"]["bbx_tool_results"][call_id].label(
                                "backup"
                            ),
                        ).where(
                            s.agent_sessions.c.task_id == tid,
                            s.agent_sessions.c.agent_id == actor,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if session is None:
            raise HTTPException(404, "Saved tool result not found")
        contents = session["contents"]
        backup = session["backup"]
        if isinstance(backup, dict):
            contents.append(backup)
        results = set()
        for content in contents:
            if (
                isinstance(content, dict)
                and content.get("type") == "function_result"
                and content.get("call_id") == call_id
            ):
                value = content.get("result")
                results.add(
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, sort_keys=True)
                )
        if not results:
            raise HTTPException(404, "Saved tool result not found")
        if len(results) != 1:
            raise HTTPException(409, "Tool call ID has conflicting saved results")
        return {"call_id": call_id, **_page(results.pop(), offset, limit)}

    async def list_deferred_messages(self, tid, actor, offset=0, limit=5):
        if actor != "lead":
            raise HTTPException(409, "Only Lead can list deferred messages")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise HTTPException(422, "Invalid deferred message page")
        async with self.repo.engine.begin() as conn:
            await self._readback_reader(conn, tid, actor)
            messages = s.ctf_messages.c
            preview = case(
                (
                    ((messages.sender_kind == "agent") & (messages.sender_id == "lead"))
                    | (messages.recipient_id == "lead"),
                    func.substr(messages.body, 1, 240),
                ),
                else_=None,
            ).label("body_preview")
            query = (
                select(
                    *[
                        messages[name]
                        for name in (
                            "id",
                            "recipient_id",
                            "sender_id",
                            "sender_kind",
                            "kind",
                            "created_at",
                            "source_turn_id",
                            "notification_purpose",
                            "status",
                            "deferred",
                        )
                    ],
                    preview,
                )
                .where(
                    messages.task_id == tid,
                    messages.status == "queued",
                    messages.deferred.is_(True),
                    messages.purpose == "execution",
                )
                .order_by(messages.recipient_id, messages.recipient_sequence, messages.id)
                .offset(offset)
                .limit(limit + 1)
            )
            found = (await conn.execute(query)).mappings().all()
        return {
            "messages": [
                {
                    key: value
                    for key, value in message.items()
                    if key != "body_preview" or value is not None
                }
                for message in found[:limit]
            ],
            "next_offset": offset + limit if len(found) > limit else None,
        }
