"""Query plans preserve paging while avoiding whole inbox/record scans."""

import json
from uuid import uuid4

import pytest
from bbx_blackboard.store import schema as s
from sqlalchemy import func, literal_column, select, text
from test_ctf_integration import ctf as ctf_fixture
from test_ctf_integration import ctf_url as ctf_url_fixture
from test_ctf_integration import team

ctf = ctf_fixture
ctf_url = ctf_url_fixture
pytestmark = pytest.mark.integration


async def test_recipient_sequence_uses_index_endpoint(ctf):
    tid = await team(ctf)
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ctf_messages (task_id,id,sender_kind,sender_id,recipient_id,"
                "recipient_sequence,kind,body,purpose,status,deferred,created_at) "
                "SELECT :tid,md5('inbox-'||n)::uuid::text,'system','system','lead',"
                "100+n,'message','Synthetic inbox','execution','delivered',false,now() "
                "FROM generate_series(1,8000) n"
            ),
            {"tid": tid},
        )
        await conn.execute(text("ANALYZE ctf_messages"))
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT max(recipient_sequence) "
                    "FROM ctf_messages WHERE task_id=:tid AND recipient_id='lead'"
                ),
                {"tid": tid},
            )
        ).scalar_one()
        assert "ix_ctf_message_sequence" in json.dumps(plan)
        assert (
            await conn.execute(
                select(func.max(s.ctf_messages.c.recipient_sequence)).where(
                    s.ctf_messages.c.task_id == tid, s.ctf_messages.c.recipient_id == "lead"
                )
            )
        ).scalar_one() == 8100


async def test_record_index_keeps_cursor_offset_and_generic_plan(ctf):
    tid = await team(ctf)
    challenge = await ctf.create_challenge(tid, "user", str(uuid4()), "Synthetic indexed records")
    cid = challenge["id"]
    async with ctf.repo.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ctf_records (task_id,id,data,version) "
                "SELECT :tid,md5('record-'||n)::uuid::text,"
                "jsonb_build_object('id',md5('record-'||n)::uuid::text,"
                "'challenge_id',CASE WHEN n%100=0 THEN :cid ELSE 'other' END,"
                "'body','Synthetic record','created_version',n),n "
                "FROM generate_series(1,12000) n"
            ),
            {"tid": tid, "cid": cid},
        )
        await conn.execute(text("ANALYZE ctf_records"))
        await conn.execute(text("SET LOCAL plan_cache_mode=force_generic_plan"))
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT data FROM ctf_records "
                    "WHERE task_id=:tid AND data->>'challenge_id'=:cid AND version>100 "
                    "ORDER BY version LIMIT 11"
                ),
                {"tid": tid, "cid": cid},
            )
        ).scalar_one()
        assert "ix_ctf_record_challenge_version" in json.dumps(plan)
    first = await ctf.list_records(tid, cid, after_version=100, limit=10)
    second = await ctf.list_records(
        tid, cid, after_version=100, offset=first["next_offset"], limit=10
    )
    assert [row["created_version"] for row in first["records"]] == list(range(200, 1200, 100))
    assert [row["created_version"] for row in second["records"]] == list(range(1200, 2200, 100))
    query = select(s.ctf_records.c.data).where(
        s.ctf_records.c.data[literal_column("'challenge_id'")].astext == cid
    )
    assert "'challenge_id'" in str(query.compile(dialect=conn.dialect))
