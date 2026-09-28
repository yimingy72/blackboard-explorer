"""PostgreSQL-bound values retain meaning without forbidden NUL characters."""

from bbx_contracts.storage import storage_safe


def test_storage_safe_recurses_without_changing_input() -> None:
    nul = chr(0)
    binary = b"raw\x00bytes"
    value = {
        "state": {"messages": [{"result": f"before{nul}after 🙂"}]},
        "history": ("正常", {f"x{nul}key": f"x{nul}y"}),
        "binary": binary,
    }
    safe = storage_safe(value)
    assert safe == {
        "state": {"messages": [{"result": "before\\u0000after 🙂"}]},
        "history": ["正常", {"x\\u0000key": "x\\u0000y"}],
        "binary": binary,
    }
    assert value["state"]["messages"][0]["result"] == f"before{nul}after 🙂"
    assert storage_safe("café 東京🙂") == "café 東京🙂"
    assert safe["binary"] is binary
