"""Commit a verified Sandbox file snapshot and optional peer Fact together."""

from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from bbx_contracts.ctf_collaboration import FileFactRequest
from bbx_contracts.models import KnowledgeAssignment
from fastapi import HTTPException

from bbx_blackboard.domain.challenge_collaboration import participants
from bbx_blackboard.domain.rules import fact_events
from bbx_blackboard.runner_control import row
from bbx_blackboard.store import schema as s


async def record_file(ctf, conn, tid, call, proof, arguments):
    source = call["data"]["sandbox_binding"]
    aid, cid = source["agent_id"], source["challenge_id"]
    task = await row(conn, s.tasks, tid)
    current = task["ctf_control"].get("native_worker_active", {}).get(aid)
    if (
        not current
        or current["assignment_ref"] != source["assignment_id"]
        or current.get("releasing")
    ):
        raise HTTPException(
            409, "The file remains an old execution result; its Fact was not recorded"
        )
    challenge = await ctf._challenge(conn, tid, cid)
    request_id = uuid5(NAMESPACE_URL, "native-file:" + call["id"])
    artifact_id = str(uuid5(NAMESPACE_URL, f"ctf-artifact:{tid}:{aid}:{request_id}"))
    fact = FileFactRequest.model_validate(arguments["fact"]) if "fact" in arguments else None
    state = (
        await ctf.repo.load(conn, tid, include_tool_calls=False, challenge_id=cid) if fact else None
    )
    origin = KnowledgeAssignment(
        agent_id=aid,
        generation=current["generation"],
        turn_id=current["turn_id"],
        epoch=current["epoch"],
        owner_id=challenge["owner_id"],
        scope_revision=current["scope_revision"],
        assignment_id=current["assignment_ref"],
    ).model_dump(mode="json")
    planned = []
    if fact is not None:
        assert state is not None
        ctf._revision(challenge, fact.expected_revision)
        iid = fact.resolves
        if iid:
            intent = state.intents.get(iid, {})
            if (
                intent.get("holder") != aid
                or (intent.get("claim_assignment_ref") or {}).get("assignment_id")
                != current["assignment_ref"]
            ):
                raise HTTPException(403, "Only the current Intent holder may close it")
        data = fact.model_dump(mode="json", exclude={"expected_revision"})
        call_id = call["data"]["invocation_id"]
        data.update(
            challenge_id=cid,
            assignment_ref=origin,
            evidence=[
                {
                    "type": "file",
                    **proof,
                    "call_id": call_id,
                    "auto": True,
                    "summary": "File snapshot at read_file time",
                }
            ],
        )
        state.tool_calls[call_id] = {"id": call_id, "agent_id": aid}
        planned = fact_events(state, aid, data, resolvable_intents={iid} if iid else set())
        for event in planned:
            event["addressed_to"] = sorted({"lead", *participants(task, cid)})
            event["payload"].update(
                challenge_id=cid,
                challenge_revision=challenge["revision"],
                scope_revision=current["scope_revision"],
            )
    registered = await ctf._board_event(
        conn,
        tid,
        aid,
        "ctf.record.appended",
        request_id,
        call["id"],
        {
            "id": artifact_id,
            "task_id": str(tid),
            "challenge_id": cid,
            "kind": "artifact_registration",
            "author_id": aid,
            "capture_call_id": call["data"]["invocation_id"],
            "assignment_ref": origin,
            "artifact": {
                "id": artifact_id,
                "task_id": str(tid),
                **proof,
                "filename": PurePosixPath(proof["path"]).name,
            },
        },
    )
    artifact = {**registered["artifact"], "created_version": registered["created_version"]}
    await ctf._board_event(
        conn,
        tid,
        aid,
        "ctf.record.appended",
        request_id,
        call["id"],
        {
            "id": str(uuid5(NAMESPACE_URL, "native-file-link:" + call["id"])),
            "task_id": str(tid),
            "challenge_id": cid,
            "author_id": aid,
            "kind": "artifact",
            "body": "Immutable file snapshot captured by read_file",
            "artifact_refs": [artifact],
        },
    )
    fact_result = None
    if planned:
        saved = await ctf.repo.append(conn, tid, planned)
        fact_result = {"id": saved[0]["object_id"], "board_version": saved[-1]["version"]}
        await ctf.repo.notify(tid, saved[-1]["version"], connection=conn)
    return {"artifact": artifact, **({"fact": fact_result} if fact_result else {})}
