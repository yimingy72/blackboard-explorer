"""Ordinary HTTP health responses and strict TLS hostname verification."""

import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import pytest
from bbx_envd import network_probe


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = f"health.p4m2.test:{cast(ThreadingHTTPServer, self.server).server_port}"
        self.send_response(
            204 if self.path == "/health" and self.headers["Host"] == expected else 404
        )
        self.end_headers()

    def log_message(self, format, *_args):
        pass


@pytest.mark.parametrize("protocol", ("http", "https"))
def test_pinned_http_status_and_tls_identity(protocol, tmp_path, monkeypatch):
    with ThreadingHTTPServer(("127.0.0.1", 0), Health) as server:
        if protocol == "https":
            openssl = shutil.which("openssl")
            if openssl is None:
                pytest.skip("Synthetic TLS certificate requires OpenSSL")
            certificate = tmp_path / "certificate.pem"
            key = tmp_path / "private.pem"
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=health.p4m2.test",
                    "-addext",
                    "subjectAltName=DNS:health.p4m2.test",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                ],
                check=True,
                capture_output=True,
            )
            listener = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            listener.load_cert_chain(certificate, key)
            server.socket = listener.wrap_socket(server.socket, server_side=True)
            # Add only the test CA; production keeps system TLS trust and hostname verification.
            trusted = ssl.create_default_context(cafile=certificate)
            monkeypatch.setattr(network_probe.ssl, "create_default_context", lambda: trusted)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = ("127.0.0.1", server.server_port, protocol, "/health")
            assert network_probe.check_probe(*args, 204, "health.p4m2.test")
            assert not network_probe.check_probe(*args, 200, "health.p4m2.test")
            if protocol == "https":
                with pytest.raises(ssl.SSLCertVerificationError):
                    network_probe.check_probe(*args, 204, "wrong.p4m2.test")
        finally:
            server.shutdown()
            thread.join(3)
