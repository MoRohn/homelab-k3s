"""tools/labzilla, the quick-start command, run for real against a fake console (plain HTTP on localhost) and a
real self-signed certificate from openssl. Covers status, the trust fingerprint check, and argument handling."""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterator

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "labzilla"
pytestmark = pytest.mark.skipif(not (shutil.which("curl") and shutil.which("openssl")), reason="needs curl and openssl")


@pytest.fixture(scope="module")
def cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    d = tmp_path_factory.mktemp("ca")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"), "-days", "1", "-subj", "/CN=Test Local CA"],
                   check=True, capture_output=True)
    fp = subprocess.run(["openssl", "x509", "-in", str(d / "c.pem"), "-noout", "-fingerprint", "-sha256"],
                        check=True, capture_output=True, text=True).stdout.split("=", 1)[1].strip()
    return (d / "c.pem").read_text(), fp


class Console:
    """A fake console: /healthz, /readyz, /api/access, /api/trust/ca.crt."""
    def __init__(self, pem: str):
        self.pem, self.ready, self.ca = pem, True, pem
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def do_GET(self) -> None:
                routes = {"/healthz": (200, "text/plain", "ok"),
                          "/readyz": (200 if outer.ready else 503, "text/plain", "ready"),
                          "/api/access": (200, "application/json", '{"secure": false, "ca_available": true, '
                                                                    '"ca_sha256": "AB:CD"}'),
                          "/api/trust/ca.crt": (200, "application/x-x509-ca-cert", outer.ca) if outer.ca else
                          (404, "text/plain", "none")}
                status, ctype, body = routes.get(self.path, (404, "text/plain", "nope"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body.encode())

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def console(cert: tuple[str, str]) -> Iterator[Console]:
    c = Console(cert[0])
    yield c
    c.srv.shutdown()


def run(*args: str, url: str | None = None, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # Hermetic: no cluster checks, and no host CA copy unless a test points at one.
    env = {**os.environ, "LABZILLA_NO_CLUSTER": "1", "NO_COLOR": "1", "LABZILLA_HOST_CA": "/nonexistent", **(extra or {})}
    cmd = [str(SCRIPT), *args] + (["--url", url] if url else [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)


def test_help_version_and_bad_arguments() -> None:
    assert "guided quick start" in run("help").stdout
    assert run("--version").stdout.startswith("labzilla ")
    r = run("bogus")
    assert r.returncode == 2 and "unknown argument" in r.stderr


def test_status_up_and_down(console: Console) -> None:
    r = run("status", url=console.url)
    assert r.returncode == 0, r.stdout
    assert "console is up" in r.stdout and "console is ready" in r.stdout and "fingerprint: AB:CD" in r.stdout
    console.ready = False
    assert "not ready yet" in run("status", url=console.url).stdout
    console.srv.shutdown()
    r = run("status", url=console.url)
    assert r.returncode == 1 and "not answering" in r.stdout


def test_trust_verifies_the_fingerprint(console: Console, cert: tuple[str, str]) -> None:
    pem, fp = cert
    ok = run("trust", "--dry-run", "--fingerprint", fp.lower(), url=console.url)     # case/format-insensitive
    assert ok.returncode == 0 and "matches --fingerprint" in ok.stdout and "nothing installed" in ok.stdout
    assert fp in ok.stdout                                                        # shown for the owner to compare
    wrong = run("trust", "--dry-run", "--fingerprint", "00:" + fp[3:], url=console.url)
    assert wrong.returncode == 1 and "does NOT match" in wrong.stdout
    unverified = run("trust", "--dry-run", url=console.url)                       # no tty, no --fingerprint
    assert unverified.returncode == 1 and "nothing installed" in unverified.stdout


def test_trust_refuses_anything_but_a_certificate(console: Console, cert: tuple[str, str]) -> None:
    console.ca = cert[0] + "\n# a PRIVATE KEY pasted here by mistake\n"
    r = run("trust", "--dry-run", "--fingerprint", cert[1], url=console.url)
    assert r.returncode == 1 and "refusing" in r.stdout
    console.ca = ""
    r = run("trust", "--dry-run", url=console.url)
    assert r.returncode == 1 and "doesn't offer its certificate" in r.stdout


def test_doctor_reports_tools(console: Console) -> None:
    r = run("doctor", url=console.url)
    assert "curl" in r.stdout and "openssl" in r.stdout


def test_trust_verifies_against_the_hosts_own_copy(console: Console, cert: tuple[str, str], tmp_path: Path) -> None:
    """On the Labzilla host, secrets/labzilla-ca.crt is the reference: a download that differs is refused."""
    host = tmp_path / "labzilla-ca.crt"
    host.write_text(cert[0])
    ok = run("trust", "--dry-run", url=console.url, extra={"LABZILLA_HOST_CA": str(host)})
    assert ok.returncode == 0 and "matches secrets/labzilla-ca.crt" in ok.stdout
    console.ca = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                                 "-nodes", "-keyout", str(tmp_path / "k2"), "-days", "1", "-subj", "/CN=Impostor"],
                                check=True, capture_output=True, text=True).stdout
    bad = run("trust", "--dry-run", url=console.url, extra={"LABZILLA_HOST_CA": str(host)})
    assert bad.returncode == 1 and "does NOT match secrets/labzilla-ca.crt" in bad.stdout
