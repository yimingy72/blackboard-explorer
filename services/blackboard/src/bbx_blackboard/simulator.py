"""Exercise a complete demo task through the public HTTP API."""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def run_demo(base_url: str, service_token: str, interval: float = 0) -> dict:
    base_url = base_url.rstrip("/")

    def call(
        method: str,
        path: str,
        *,
        token: str = service_token,
        body: dict | None = None,
        raw: bytes | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        data = raw
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            headers["Content-Type"] = "application/octet-stream"
        request = Request(base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                status, content = response.status, response.read()
        except HTTPError as exc:
            status, content = exc.code, exc.read()
        result = json.loads(content) if content else None
        if status not in expected:
            raise RuntimeError(f"{method} {path} returned {status}: {result}")
        if interval:
            time.sleep(interval)
        return status, result

    _, created = call(
        "POST",
        "/api/tasks",
        body={
            "goal": "Find and verify the cause of a gateway failure",
            "acceptance": [{"id": "A1", "desc": "Show a supported root cause"}],
            "budget": {"max_cost": "10", "max_minutes": 60},
            "agent_profile": "default",
        },
    )
    tid = created["id"]
    task = f"/api/tasks/{tid}"
    call("POST", f"{task}/start")
    call("POST", f"{task}/status", body={"status": "running"})

    agents = []
    for index in range(3):
        _, registered = call(
            "POST", f"{task}/agents", body={"task_type": "explore", "is_seed": index == 0}
        )
        agents.append(registered)

    def upload(agent_id: str, name: str) -> str:
        key = f"evidence/{tid}/{agent_id}/{name}.txt"
        call("POST", f"{task}/uploads?{urlencode({'key': key})}", raw=name.encode())
        return key

    def post_fact(agent: dict[str, Any], name: str, statement: str, **extra) -> str:
        uri = upload(agent["agent_id"], name)
        _, posted = call(
            "POST",
            f"{task}/facts",
            token=agent["token"],
            body={
                "kind": "observation",
                "statement": statement,
                "evidence": [{"type": "text", "uri": uri, "summary": name}],
                **extra,
            },
        )
        evidence_uris.append(uri)
        return posted["id"]

    def finish(agent_id: str, receipt: dict[str, Any]) -> None:
        call(
            "POST",
            f"{task}/agents/{agent_id}/finish",
            body={"receipt": receipt, "end_reason": "normal"},
        )

    evidence_uris: list[str] = []
    first = post_fact(agents[0], "gateway-error", "The gateway returned 502")
    _, proposed = call(
        "POST",
        f"{task}/intents",
        token=agents[0]["token"],
        body={
            "statement": "Trace the upstream failure",
            "based_on": [first],
            "expected": "Identify a repeatable cause",
            "method": "Inspect gateway and upstream traces",
            "relates_to": ["A1"],
        },
    )
    intent_id = proposed["id"]

    def compete(agent: dict[str, Any]) -> tuple[str, int]:
        status, _ = call(
            "POST",
            f"{task}/intents/{intent_id}/claim",
            token=agent["token"],
            expected=(200, 422),
        )
        return agent["agent_id"], status

    with ThreadPoolExecutor(max_workers=2) as workers:
        claims = list(workers.map(compete, agents[1:]))
    winners = [aid for aid, status in claims if status == 200]
    if len(winners) != 1 or sorted(status for _, status in claims) != [200, 422]:
        raise RuntimeError(f"claim race did not select one holder: {claims}")
    winner = next(agent for agent in agents if agent["agent_id"] == winners[0])
    loser = next(agent for agent in agents[1:] if agent is not winner)

    root_cause = post_fact(
        winner,
        "upstream-trace",
        "A slow upstream query exhausted the connection pool",
        resolves=intent_id,
        result="confirmed",
        satisfies=["A1"],
    )

    def judge(verdict: str, reason: str, *, missing: str | None = None) -> None:
        _, close_agent = call(
            "POST", f"{task}/agents", body={"task_type": "close", "close_mode": "judge"}
        )
        item: dict[str, Any] = {"id": "A1", "verdict": verdict, "reason": reason}
        if verdict == "met":
            item["evidence_facts"] = [root_cause]
        else:
            item["missing"] = missing
        call("POST", f"{task}/close", token=close_agent["token"], body={"verdicts": [item]})
        finish(
            close_agent["agent_id"],
            {"accepted": True, "data": {"note": f"Judged A1 {verdict}"}},
        )

    judge("met", "The trace supports the cause")
    challenge = post_fact(loser, "counterexample", "The trace may be stale", disputes=[root_cause])
    judge("unmet", "The supporting trace is disputed", missing="Resolve the dispute")
    counter = post_fact(
        agents[0], "fresh-trace", "A fresh trace refutes the challenge", disputes=[challenge]
    )
    judge("met", "The counter-dispute restores the supported trace")

    call("POST", f"{task}/status", body={"status": "closing"})
    for agent in agents:
        call(
            "POST",
            f"{task}/agents/{agent['agent_id']}/conclude",
            body={"reason": "closing"},
        )
    posted_by = {
        agents[0]["agent_id"]: [first, intent_id, counter],
        winner["agent_id"]: [root_cause],
        loser["agent_id"]: [challenge],
    }
    for agent in agents:
        aid = agent["agent_id"]
        finish(
            aid,
            {
                "accepted": True,
                "data": {
                    "intent_result": "confirmed" if aid == winner["agent_id"] else "none",
                    "posted": posted_by[aid],
                    "note": "Demo exploration handoff complete",
                },
            },
        )
    report_uri = f"reports/{tid}.md"
    report = f"# Final report\n\nThe upstream query exhausted the pool. Evidence: {root_cause}.\n"
    call("POST", f"{task}/uploads?{urlencode({'key': report_uri})}", raw=report.encode())
    _, final_agent = call(
        "POST", f"{task}/agents", body={"task_type": "close", "close_mode": "final"}
    )
    call(
        "POST",
        f"{task}/close",
        token=final_agent["token"],
        body={
            "verdicts": [
                {
                    "id": "A1",
                    "verdict": "met",
                    "reason": "The supported cause is reproducible",
                    "evidence_facts": [root_cause],
                }
            ],
            "report_uri": report_uri,
            "report": report,
        },
    )
    finish(final_agent["agent_id"], {"accepted": True, "data": {"note": "Final report submitted"}})
    _, events = call("GET", f"{task}/events")
    return {
        "task_id": tid,
        "agent_ids": [agent["agent_id"] for agent in agents],
        "claim_results": claims,
        "claim_winner": winners[0],
        "fact_ids": [first, root_cause, challenge, counter],
        "intent_id": intent_id,
        "evidence_uris": evidence_uris,
        "report_uri": report_uri,
        "event_versions": [event["version"] for event in events],
        "event_types": [event["type"] for event in events],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a blackboard HTTP demo")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--scenario", choices=["demo"], default="demo")
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()
    if args.interval < 0:
        parser.error("--interval must be nonnegative")
    token = os.environ.get("SERVICE_TOKEN")
    if not token:
        parser.error("SERVICE_TOKEN is required")
    print(json.dumps(run_demo(args.base_url, token, args.interval), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
