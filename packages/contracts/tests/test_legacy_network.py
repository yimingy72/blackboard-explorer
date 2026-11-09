"""Protected health probes are fixed-origin, bounded application requests."""

import pytest
from bbx_contracts.legacy_network import LegacyGatewayProbe
from pydantic import ValidationError


def test_legacy_health_probe_defaults_and_rejects_alternate_origins():
    address = {"ipv4": "10.80.0.5", "port": 443}
    assert LegacyGatewayProbe.model_validate(address).model_dump() == {
        **address,
        "scheme": "http",
        "path": "/",
        "status": 200,
        "server_name": None,
    }
    tls = LegacyGatewayProbe.model_validate(
        {
            **address,
            "scheme": "https",
            "path": "/live",
            "status": 204,
            "server_name": "probe.test",
        }
    )
    assert (tls.ipv4, tls.server_name, tls.status) == ("10.80.0.5", "probe.test", 204)
    for supplied in (
        {"path": "//another.test"},
        {"path": "/status?token=credential"},
        {"path": "/a/../status"},
        {"path": "/status\r\nHost:example.test"},
        {"server_name": "identity@probe.test"},
        {"status": 101},
    ):
        with pytest.raises(ValidationError):
            LegacyGatewayProbe.model_validate({**address, **supplied})
