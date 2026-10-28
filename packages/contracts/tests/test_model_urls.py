"""Model endpoint routing is independent of stored configuration and credentials."""

import pytest
from bbx_contracts import model_urls
from bbx_contracts.model_urls import resolve_model_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:3425/v1", "http://host.docker.internal:3425/v1"),
        ("http://127.24.5.6:3425/v1", "http://host.docker.internal:3425/v1"),
        (
            "https://LOCALHOST:8443/gateway/v1?api=1#part",
            "https://host.docker.internal:8443/gateway/v1?api=1#part",
        ),
        ("http://[::1]:3425/v1", "http://host.docker.internal:3425/v1"),
        ("http://[0:0:0:0:0:0:0:1]/v1", "http://host.docker.internal/v1"),
        ("http://localhost/v1", "http://host.docker.internal/v1"),
        ("http://host.docker.internal:3425/v1", "http://host.docker.internal:3425/v1"),
        ("https://models.example/v1?next=localhost", "https://models.example/v1?next=localhost"),
        ("http://192.168.1.2/v1", "http://192.168.1.2/v1"),
        ("http://[2001:db8::1]:3425/v1", "http://[2001:db8::1]:3425/v1"),
        ("http://localhost.example/v1", "http://localhost.example/v1"),
        ("provider-default", "provider-default"),
    ],
)
def test_model_url_routes_loopback_only_in_containers(url, expected):
    assert resolve_model_url(url, containerized=True) == expected
    assert resolve_model_url(url, containerized=False) == url


@pytest.mark.parametrize("exists", [False, True])
def test_container_detection_uses_docker_marker(monkeypatch, exists):
    def marker_exists(path):
        assert str(path) == "/.dockerenv"
        return exists

    monkeypatch.setattr(model_urls.Path, "exists", marker_exists)
    assert model_urls.in_container() is exists
