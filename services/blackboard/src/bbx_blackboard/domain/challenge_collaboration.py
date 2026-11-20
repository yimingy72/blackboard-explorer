"""Peer membership, availability and wall-clock rules without execution side effects."""

from datetime import datetime, timedelta

from bbx_contracts.ctf_collaboration import (
    ASSISTANCE_REMINDER_MS,
    MAX_CHALLENGE_WORKERS,
    MAX_WORKER_INTENTS,
    ChallengeCollaboration,
)

from .rules import RuleViolation, event


def collaboration(challenge):
    return ChallengeCollaboration.model_validate(challenge.get("collaboration") or {})


def participants(task, cid):
    return {
        aid: item
        for aid, item in task["ctf_control"].get("native_worker_active", {}).items()
        if item["work_id"] == str(cid)
    }


def assistance_count(task, challenge):
    return sum(aid != challenge.get("owner_id") for aid in participants(task, challenge["id"]))


def assistance_slots(task, challenge):
    if challenge["work_status"] != "in_progress" or not challenge.get("owner_id"):
        return 0
    return max(0, collaboration(challenge).assistance_target - assistance_count(task, challenge))


def readable(task, challenge, aid):
    return not challenge.get("tombstone") and (
        aid == "lead"
        or aid in participants(task, challenge["id"])
        or challenge["work_status"] == "pending"
        and not challenge.get("owner_id")
        or assistance_slots(task, challenge) > 0
    )


def available_work(task, challenges):
    ready = task["ctf_control"].get("native_worker_ready", {})
    new, assistance = [], []
    for challenge in challenges:
        if challenge.get("tombstone"):
            continue
        cid = challenge["id"]
        item = {
            "challenge_id": cid,
            "revision": challenge["revision"],
            "title": challenge["title"],
            "description": challenge.get("description", ""),
            "difficulty": collaboration(challenge).difficulty,
        }
        queued = ready.get(cid, {})
        if (
            challenge["work_status"] == "pending"
            and not challenge.get("owner_id")
            and queued.get("readiness") in {"ready", "no_external_materials"}
        ):
            new.append(item)
        elif slots := assistance_slots(task, challenge):
            assistance.append({**item, "open_slots": slots})
    new.sort(
        key=lambda item: (
            ready[item["challenge_id"]]["priority"],
            ready[item["challenge_id"]]["sequence"],
        )
    )
    assistance.sort(key=lambda item: item["challenge_id"])
    return {"new_challenges": new, "assistance": assistance}


def require_claimable(task, challenge, member, choices):
    aid = member["id"]
    if (
        member["role"] != "teammate"
        or member["lifecycle"] != "active"
        or member["run_state"] != "idle"
        or member.get("pending_operation")
        or aid in task["ctf_control"].get("native_worker_active", {})
    ):
        raise RuleViolation("worker_not_idle", "Worker 当前不可领取新工作。")
    preferred = choices["new_challenges"] or choices["assistance"]
    if not any(item["challenge_id"] == challenge["id"] for item in preferred):
        raise RuleViolation("work_changed", "可领取工作已变化；优先选择未领取的新题。")
    team = participants(task, challenge["id"])
    if len(team) >= MAX_CHALLENGE_WORKERS:
        raise RuleViolation("team_full", "该题已经有 10 名 Worker。")


def claim_challenge(task, challenge, aid, now):
    state = collaboration(challenge)
    owner = challenge.get("owner_id")
    assistants = [member for member in participants(task, challenge["id"]) if member != owner]
    if owner is None:
        owner = aid
        state.solve_started_at = now
    else:
        assistants.append(aid)
    return {
        **challenge,
        "owner_id": owner,
        "collaborator_ids": sorted(set(assistants)),
        "work_status": "in_progress",
        "dispatch_status": "assigned",
        "requested_worker_id": None,
        "revision": challenge["revision"] + 1,
        "collaboration": state.model_dump(mode="json"),
    }


def clock_state(challenge, now: datetime, *, running: bool):
    state = collaboration(challenge)
    if state.solve_started_at is not None:
        elapsed = max(0, int((now - state.solve_started_at).total_seconds() * 1000))
        state.solve_elapsed_ms += elapsed
    state.solve_started_at = now if running and challenge["work_status"] == "in_progress" else None
    return state


def reminder_deadline(challenge):
    state = collaboration(challenge)
    if state.assistance_reminded or state.solve_started_at is None:
        return None
    return state.solve_started_at + timedelta(
        milliseconds=max(0, ASSISTANCE_REMINDER_MS - state.solve_elapsed_ms)
    )


def due_reminder(task, challenge, now: datetime):
    deadline = reminder_deadline(challenge)
    if (
        task["status"] != "running"
        or challenge["work_status"] != "in_progress"
        or deadline is None
        or now < deadline
    ):
        return None
    state = clock_state(challenge, now, running=True)
    state.assistance_reminded = True
    if assistance_count(task, challenge) == 0:
        state.assistance_target = max(1, state.assistance_target)
    return {
        **challenge,
        "collaboration": state.model_dump(mode="json"),
        "revision": challenge["revision"] + 1,
    }


def claim_intent_events(state, iid, aid, assignment, expected_version):
    intent = state.intents.get(iid)
    if intent is None:
        raise RuleViolation("invalid_reference", "意图不在当前题目中。")
    if intent["version"] != expected_version or intent["status"] != "open":
        raise RuleViolation("intent_unavailable", "意图已被认领或更新，请重新选择。")
    held = sum(
        item["status"] == "claimed" and item["holder"] == aid for item in state.intents.values()
    )
    if held >= MAX_WORKER_INTENTS:
        raise RuleViolation("already_holding", "已持有 3 条意图，完成后再领取。")
    return [
        event(
            "intent.claimed",
            aid,
            {
                "intent_id": iid,
                "holder": aid,
                "challenge_id": intent["challenge_id"],
                "claim_assignment_ref": assignment,
            },
            iid,
        )
    ]
