"""The Labzilla Local CA certificate download (/api/trust/ca.crt) and its fingerprint in /api/access.

A new device needs the certificate before it can trust the console, so the route is open. It must only
ever serve a certificate: a file that also holds a private key is refused."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lif.console import errors
from lif.console.routes import auth as auth_routes

DER = b"\x30\x82\x01\x0a" + bytes(range(200))          # stand-in bytes; only the PEM framing is parsed
PEM = ("-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(DER).decode() + "-----END CERTIFICATE-----\n")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(auth_routes, "CA_FILE", tmp_path / "ca.crt")
    app = FastAPI()
    errors.install(app)
    app.include_router(auth_routes.router)
    return TestClient(app)


def _fp(der: bytes) -> str:
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def test_download_and_fingerprint(client: TestClient) -> None:
    auth_routes.CA_FILE.write_text(PEM)
    r = client.get("/api/trust/ca.crt")
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-x509-ca-cert"
    assert 'filename="labzilla-ca.crt"' in r.headers["content-disposition"]
    assert r.text.startswith("-----BEGIN CERTIFICATE-----") and r.headers["x-labzilla-ca-sha256"] == _fp(DER)
    a = client.get("/api/access").json()
    assert a["ca_available"] is True and a["ca_sha256"] == _fp(DER)


def test_not_offered_when_missing(client: TestClient) -> None:
    assert client.get("/api/trust/ca.crt").status_code == 404
    a = client.get("/api/access").json()
    assert a["ca_available"] is False and a["ca_sha256"] is None


def test_never_serves_a_file_with_a_private_key(client: TestClient) -> None:
    # The refusal keys on the words alone, so no key-shaped block is needed (or committed) to test it.
    auth_routes.CA_FILE.write_text(PEM + "\n# a PRIVATE KEY was pasted here by mistake\n")
    assert client.get("/api/trust/ca.crt").status_code == 404
    assert client.get("/api/access").json()["ca_available"] is False
