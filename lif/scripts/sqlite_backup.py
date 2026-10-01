#!/usr/bin/env python3
"""Nightly backup of LIF's local-path SQLite databases to MinIO (CronJob lif-sqlite-backup).

The decision-fabric PVC (`lif-decisions`) is on local-path, which Longhorn cannot back up. It
holds decisions.db (the decision log) and decision-eng.db (Decision Engineering: releases,
provenance, shadow rows, human queue), plus calibration reports.

  backup          for every *.db under $SRC: SQLite online-backup API (consistent while the
                  service writes, WAL included) → PRAGMA integrity_check → gzip → PUT
                  s3://$BUCKET/$PREFIX/<UTC date>/<name>.gz, plus calibration.tar.gz and a
                  manifest with sha256s. Then deletes dates older than $RETAIN_DAYS.
  verify-latest   download the newest set, check sha256 and integrity_check each database.

Standard library only (SigV4 signing included), so it runs in the existing LIF image.
Env: S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, BUCKET (longhorn-backups), PREFIX (lif-sqlite),
     SRC (/data), RETAIN_DAYS (14), S3_REGION (us-east-1).
Off-site: backup/offsite-sync.sh mirrors the whole bucket, so these copies leave the host too.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

EMPTY_SHA = hashlib.sha256(b"").hexdigest()


# ── S3 (SigV4, path-style) ────────────────────────────────────────────────────

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, access: str, secret: str, region: str, payload_sha: str,
                  now: dt.datetime | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    u = urllib.parse.urlsplit(url)
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": u.netloc, "x-amz-content-sha256": payload_sha, "x-amz-date": amz_date,
               **{k.lower(): v for k, v in (extra or {}).items()}}
    signed = ";".join(sorted(headers))
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                     for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([method, urllib.parse.quote(u.path or "/", safe="/-_.~"), query,
                           "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers)), signed, payload_sha])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(("AWS4" + secret).encode(), day), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return headers


class S3:
    def __init__(self, endpoint: str, access: str, secret: str, bucket: str, region: str = "us-east-1"):
        self.base, self.access, self.secret = endpoint.rstrip("/"), access, secret
        self.bucket, self.region = bucket, region

    def _req(self, method: str, key: str = "", query: str = "", body: bytes = b"") -> bytes:
        url = f"{self.base}/{self.bucket}/{urllib.parse.quote(key, safe='/-_.~')}" + (f"?{query}" if query else "")
        h = sigv4_headers(method, url, self.access, self.secret, self.region, hashlib.sha256(body).hexdigest())
        h.pop("host")
        req = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=h)
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def put(self, key: str, body: bytes) -> None:
        self._req("PUT", key, body=body)

    def get(self, key: str) -> bytes:
        return self._req("GET", key)

    def delete(self, key: str) -> None:
        self._req("DELETE", key)

    def list(self, prefix: str) -> list[str]:
        keys, token = [], None
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._req("GET", query=urllib.parse.urlencode(q)))
            ns = {"s": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
            p = "s:" if ns else ""
            keys += [e.text for e in root.findall(f".//{p}Contents/{p}Key", ns)]
            if (root.findtext(f"{p}IsTruncated", default="false", namespaces=ns) or "").lower() != "true":
                return keys
            token = root.findtext(f"{p}NextContinuationToken", namespaces=ns)


# ── backup / verify ──────────────────────────────────────────────────────────

def snapshot(db: Path, out: Path) -> str:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)                               # consistent point-in-time copy, WAL included
    finally:
        src.close()
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    if ok != "ok":
        raise RuntimeError(f"{db.name}: integrity_check on the copy returned {ok!r}")
    return ok


def backup(s3: S3, src: Path, prefix: str, retain_days: int) -> dict:
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    manifest = {"created": dt.datetime.now(dt.timezone.utc).isoformat(), "files": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for db in sorted(p for p in src.glob("*.db") if p.is_file()):
            copy = Path(tmp) / db.name
            snapshot(db, copy)
            blob = gzip.compress(copy.read_bytes(), 6)
            key = f"{prefix}/{day}/{db.name}.gz"
            s3.put(key, blob)
            manifest["files"][db.name] = {"key": key, "bytes": copy.stat().st_size,
                                          "sha256": hashlib.sha256(copy.read_bytes()).hexdigest()}
            print(f"backed up {db.name}: {copy.stat().st_size} bytes → {key}", flush=True)
        cal = src / "calibration"
        if cal.is_dir() and any(cal.iterdir()):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                t.add(cal, arcname="calibration")
            key = f"{prefix}/{day}/calibration.tar.gz"
            s3.put(key, buf.getvalue())
            manifest["files"]["calibration.tar.gz"] = {"key": key, "sha256": hashlib.sha256(buf.getvalue()).hexdigest()}
    if not manifest["files"]:
        raise RuntimeError(f"no SQLite databases found under {src}")
    s3.put(f"{prefix}/{day}/manifest.json", json.dumps(manifest, indent=1).encode())
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retain_days)).strftime("%Y-%m-%d")
    pruned = 0
    for key in s3.list(prefix + "/"):
        d = key[len(prefix) + 1:].split("/", 1)[0]
        if len(d) == 10 and d < cutoff:
            s3.delete(key)
            pruned += 1
    print(f"done: {len(manifest['files'])} files for {day}; pruned {pruned} objects older than {cutoff}", flush=True)
    return manifest


def verify_latest(s3: S3, prefix: str) -> dict:
    days = sorted({k[len(prefix) + 1:].split("/", 1)[0] for k in s3.list(prefix + "/") if k.endswith("manifest.json")})
    if not days:
        raise RuntimeError("no backups found")
    m = json.loads(s3.get(f"{prefix}/{days[-1]}/manifest.json"))
    out = {"day": days[-1], "files": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for name, f in m["files"].items():
            raw = s3.get(f["key"])
            if name.endswith(".db"):
                data = gzip.decompress(raw)
                assert hashlib.sha256(data).hexdigest() == f["sha256"], f"{name}: sha256 mismatch"
                p = Path(tmp) / name
                p.write_bytes(data)
                c = sqlite3.connect(p)
                ok = c.execute("PRAGMA integrity_check").fetchone()[0]
                tables = c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                c.close()
                out["files"][name] = {"integrity": ok, "tables": tables, "bytes": len(data)}
            else:
                assert hashlib.sha256(raw).hexdigest() == f["sha256"], f"{name}: sha256 mismatch"
                out["files"][name] = {"sha256": "ok"}
    print(json.dumps(out, indent=1))
    return out


def main() -> int:
    e = os.environ
    s3 = S3(e["S3_ENDPOINT"], e["S3_ACCESS_KEY"], e["S3_SECRET_KEY"], e.get("BUCKET", "longhorn-backups"),
            e.get("S3_REGION", "us-east-1"))
    prefix = e.get("PREFIX", "lif-sqlite").strip("/")
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "verify-latest":
            verify_latest(s3, prefix)
        else:
            backup(s3, Path(e.get("SRC", "/data")), prefix, int(e.get("RETAIN_DAYS", "14")))
    except (urllib.error.URLError, RuntimeError, AssertionError, sqlite3.Error) as err:
        print(f"backup failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
