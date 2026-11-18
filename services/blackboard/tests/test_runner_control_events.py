"""V1 native delivery metadata follows original mailbox visibility."""

from bbx_blackboard.service import ctf_agent_event


def test_v1_delivery_preserves_sender_visibility_without_opening_other_events():
    event = {
        "version": 17,
        "type": "ctf.message.delivered",
        "actor": "worker",
        "object_id": "input",
        "addressed_to": ["lead", "worker"],
        "payload": {
            "id": "input",
            "binding_id": "binding",
            "native_entry_id": "entry",
            "native_seq": 4,
            "claim_token": "private-claim",
        },
    }
    sender = ctf_agent_event(event, "lead", versioned=True)
    assert sender["payload"]["native_entry_id"] == "entry"
    assert "claim_token" not in sender["payload"]
    for identity, addressed in (("observer", event["addressed_to"]), ("lead", None)):
        hidden = ctf_agent_event({**event, "addressed_to": addressed}, identity, versioned=True)
        assert (
            hidden["type"] == "ctf.cursor" and hidden["payload"] == {} and hidden["version"] == 17
        )
    assert ctf_agent_event(event, "lead")["type"] == "ctf.cursor"
