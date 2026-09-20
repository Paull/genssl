#!/usr/bin/env python3
"""Single-machine HTTP API for CornTech self-signed certificates.

The server deliberately delegates certificate issuance to the existing shell
scripts so CLI and HTTP generated artifacts stay compatible.  It only adds a
small SQLite catalog and a read/download API around the files in ``out/``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

ROOT_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT_DIR / "out"
DB_PATH = OUT_DIR / "certificates.db"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}$")
ALLOWED_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class CertificateError(Exception):
    status = HTTPStatus.BAD_REQUEST


class CertificateStore:
    """SQLite catalog and issuance coordinator.

    ``issue_lock`` protects OpenSSL's shared ``out/serial`` files and avoids
    two shell scripts updating the CA database at the same time.
    """

    def __init__(self, root_dir: Path = ROOT_DIR, db_path: Path = DB_PATH):
        self.root_dir = Path(root_dir).resolve()
        self.out_dir = self.root_dir / "out"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.issue_lock = threading.Lock()
        self.db_lock = threading.Lock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.db:
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS certificates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cert_type TEXT NOT NULL CHECK(cert_type IN ('client','server')),
                    name TEXT NOT NULL,
                    sans_json TEXT NOT NULL DEFAULT '[]',
                    days INTEGER NOT NULL,
                    output_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'issued'
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_cert_name ON certificates(name)")
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS registrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_registration_name ON registrations(name)")

    def close(self) -> None:
        self.db.close()

    def _row_json(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["sans"] = json.loads(item.pop("sans_json") or "[]")
        item["files"] = self.files_for_dir(item["output_dir"])
        # Keep the storage-oriented ``cert_type`` field while exposing the
        # names used by the browser and the REST documentation.
        item["type"] = item["cert_type"]
        item["kind"] = item["cert_type"]
        item["key_type"] = "both" if any(name.startswith("ec_") for name in item["files"]) and any(name.startswith("rsa_") for name in item["files"]) else ("ec" if any(name.startswith("ec_") for name in item["files"]) else "rsa")
        try:
            issued = _dt.datetime.fromisoformat(item["created_at"])
            item["expires_at"] = (issued + _dt.timedelta(days=int(item["days"]))).isoformat()
        except (TypeError, ValueError):
            item["expires_at"] = None
        item["download_url"] = f"/api/certificates/{item['id']}/download"
        return item

    def list_certificates(self, cert_type: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM certificates"
        params: tuple[Any, ...] = ()
        if cert_type:
            query += " WHERE cert_type = ?"
            params = (cert_type,)
        query += " ORDER BY id DESC"
        with self.db_lock:
            rows = self.db.execute(query, params).fetchall()
        return [self._row_json(r) for r in rows]

    def get_certificate(self, cert_id: int) -> Optional[dict[str, Any]]:
        with self.db_lock:
            row = self.db.execute("SELECT * FROM certificates WHERE id = ?", (cert_id,)).fetchone()
        return self._row_json(row) if row else None

    def list_registrations(self) -> list[dict[str, Any]]:
        with self.db_lock:
            rows = self.db.execute("SELECT * FROM registrations ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def create_registration(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name") or payload.get("app")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
            raise CertificateError("registration name must be 1-128 characters")
        owner = payload.get("owner", "")
        notes = payload.get("notes", "")
        if not isinstance(owner, str) or not isinstance(notes, str):
            raise CertificateError("owner and notes must be strings")
        with self.db_lock, self.db:
            cur = self.db.execute(
                "INSERT INTO registrations(name,owner,notes,created_at) VALUES(?,?,?,?)",
                (name.strip(), owner[:256], notes[:2000], _utc_now()),
            )
            registration_id = int(cur.lastrowid)
            row = self.db.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,)).fetchone()
        return dict(row) if row else {}

    def files_for_dir(self, relative_dir: str) -> list[str]:
        directory = self._safe_output_dir(relative_dir)
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.is_file())

    def _safe_output_dir(self, relative_dir: str) -> Path:
        candidate = (self.root_dir / relative_dir).resolve()
        out_root = self.out_dir.resolve()
        if candidate != out_root and out_root not in candidate.parents:
            raise CertificateError("output directory is outside out/")
        return candidate

    @staticmethod
    def _validate_name(value: Any, field: str = "name") -> str:
        if not isinstance(value, str) or not NAME_RE.fullmatch(value):
            raise CertificateError(f"{field} must contain 1-128 letters, digits, dot, underscore or hyphen")
        return value

    @staticmethod
    def _validate_sans(values: Any) -> list[str]:
        if values is None or values == []:
            return []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values or len(values) > 64:
            raise CertificateError("sans must be a non-empty list with at most 64 entries")
        output = []
        for value in values:
            if not isinstance(value, str) or not SAN_RE.fullmatch(value):
                raise CertificateError("each SAN must be a hostname or IP address")
            output.append(value)
        return list(dict.fromkeys(output))

    def _root_available(self) -> bool:
        return (self.out_dir / "rsa_root.crt").is_file() and (self.out_dir / "ec_root.crt").is_file()

    def _ensure_ca_state(self) -> None:
        """Create the OpenSSL CA database files used by gen_server_cert.sh."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        required = (self.out_dir / "newcerts", self.out_dir / "index.txt", self.out_dir / "index.txt.attr", self.out_dir / "serial")
        if not (required[0].is_dir() and all(path.is_file() for path in required[1:])):
            (self.out_dir / "newcerts").mkdir(parents=True, exist_ok=True)
            (self.out_dir / "index.txt").touch()
            (self.out_dir / "index.txt.attr").write_text("unique_subject = no\n", encoding="utf-8")
            (self.out_dir / "serial").write_text("1000\n", encoding="utf-8")

    def issue(self, payload: dict[str, Any], rootpass: Optional[str] = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CertificateError("request body must be a JSON object")
        cert_type = payload.get("type", payload.get("kind", "server"))
        if cert_type not in ("client", "server"):
            raise CertificateError("type must be client or server")
        key_type = payload.get("key_type", "both")
        if key_type not in ("rsa", "ec", "both"):
            raise CertificateError("key_type must be rsa, ec or both")
        sans = self._validate_sans(payload.get("sans", payload.get("domains"))) if cert_type == "server" else []
        name = payload.get("name")
        if name is None and sans:
            name = sans[0]
        name = self._validate_name(name)
        if cert_type == "server" and not sans:
            sans = [name]
        elif cert_type == "server":
            # The legacy server script uses its first argument as the output
            # directory/CN and also includes every argument in the SAN list.
            # Keep the API record aligned with that behavior when the display
            # name differs from the first explicitly entered SAN.
            sans = list(dict.fromkeys([name, *sans]))
        default_days = 365 if cert_type == "client" else 398
        try:
            days = int(payload.get("days", default_days))
        except (TypeError, ValueError):
            raise CertificateError("days must be an integer")
        if days < 1 or days > 7300:
            raise CertificateError("days must be between 1 and 7300")
        if not self._root_available():
            err = CertificateError("CA root certificates are missing; initialize /api/ca first")
            err.status = HTTPStatus.CONFLICT
            raise err
        env = os.environ.copy()
        secret = rootpass if rootpass is not None else (env.get("ROOTPASS") or env.get("rootpass"))
        if not secret:
            err = CertificateError("ROOTPASS environment variable is required")
            err.status = HTTPStatus.CONFLICT
            raise err
        env["rootpass"] = secret
        env["days"] = str(days)
        script = self.root_dir / ("gen_client_cert.sh" if cert_type == "client" else "gen_server_cert.sh")
        args = ["bash", str(script), name] if cert_type == "client" else ["bash", str(script), *sans]
        with self.issue_lock:
            before = set((self.out_dir / name).glob("*") if (self.out_dir / name).is_dir() else ())
            proc = subprocess.run(args, cwd=self.root_dir, env=env, text=True, capture_output=True, timeout=180)
            if proc.returncode != 0:
                raise CertificateError(f"certificate script failed: {proc.stderr[-800:] or proc.stdout[-800:]}")
            generated = self._find_generated_dir(name, before)
            if generated is None:
                raise CertificateError("certificate script completed without output files")
            expected = (f"rsa_{name}.crt", f"rsa_{name}.key", f"ec_{name}.crt", f"ec_{name}.key")
            missing = [filename for filename in expected if not (generated / filename).is_file()]
            if missing:
                raise CertificateError(f"certificate script did not create required files: {', '.join(missing)}")
            # Keep an immutable, uniquely named API artifact even though the
            # legacy scripts group output by minute and may reuse that folder.
            api_dir = generated.parent / f"api-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            shutil.copytree(generated, api_dir)
            if key_type != "both":
                unwanted = "ec_" if key_type == "rsa" else "rsa_"
                for artifact in api_dir.iterdir():
                    if artifact.name.startswith(unwanted):
                        artifact.unlink()
        relative = str(api_dir.relative_to(self.root_dir))
        with self.db_lock, self.db:
            cur = self.db.execute(
                "INSERT INTO certificates(cert_type,name,sans_json,days,output_dir,created_at,status) VALUES(?,?,?,?,?,?,?)",
                (cert_type, name, json.dumps(sans), days, relative, _utc_now(), "issued"),
            )
            cert_id = int(cur.lastrowid)
        return self.get_certificate(cert_id) or {}

    def _find_generated_dir(self, name: str, before: set[Path]) -> Optional[Path]:
        base = self.out_dir / name
        if not base.is_dir():
            return None
        dirs = [p for p in base.iterdir() if p.is_dir() and not p.name.startswith("api-")]
        changed = [p for p in dirs if p not in before]
        candidates = changed or dirs
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            if any(path.glob("*.crt")) and any(path.glob("*.key")):
                return path
        return None

    def initialize_ca(self, rootpass: Optional[str], force: bool = False) -> dict[str, Any]:
        if not rootpass:
            err = CertificateError("rootpass is required to initialize the CA")
            err.status = HTTPStatus.CONFLICT
            raise err
        self._ensure_ca_state()
        if self._root_available() and not force:
            return self.ca_status()
        if force or not self._root_available():
            for filename in ("rsa_root.crt", "rsa_root.key", "ec_root.crt", "ec_root.key"):
                (self.out_dir / filename).unlink(missing_ok=True)
        env = os.environ.copy()
        env["rootpass"] = rootpass
        proc = subprocess.run(["bash", str(self.root_dir / "gen_root_cert.sh")], cwd=self.root_dir, env=env, text=True, capture_output=True, timeout=180)
        if proc.returncode != 0 or not self._root_available():
            raise CertificateError(f"CA initialization failed: {proc.stderr[-800:] or proc.stdout[-800:]}")
        return self.ca_status()

    def ca_status(self) -> dict[str, Any]:
        roots = []
        for filename in ("rsa_root.crt", "ec_root.crt"):
            path = self.out_dir / filename
            roots.append({"name": filename, "available": path.is_file(), "path": str(path.relative_to(self.root_dir))})
        return {"available": all(x["available"] for x in roots), "roots": roots}

    def artifact(self, cert_id: int, filename: str) -> tuple[Path, str]:
        record = self.get_certificate(cert_id)
        if not record:
            raise KeyError(cert_id)
        if not ALLOWED_ARTIFACT_RE.fullmatch(filename) or filename not in record["files"]:
            raise FileNotFoundError(filename)
        directory = self._safe_output_dir(record["output_dir"])
        path = (directory / filename).resolve()
        if path.parent != directory.resolve() or not path.is_file():
            raise FileNotFoundError(filename)
        return path, filename

    def archive(self, cert_id: int) -> tuple[bytes, str]:
        record = self.get_certificate(cert_id)
        if not record:
            raise KeyError(cert_id)
        directory = self._safe_output_dir(record["output_dir"])
        if not directory.is_dir():
            raise FileNotFoundError(record["output_dir"])
        filename = f"{record['cert_type']}-{record['name']}-{record['id']}.zip"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in directory.iterdir():
                if item.is_file():
                    archive.write(item, item.name)
        return stream.getvalue(), filename


class APIHandler(BaseHTTPRequestHandler):
    server_version = "CornCert/1.0"

    @property
    def store(self) -> CertificateStore:
        return self.server.store  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str, filename: Optional[str] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _static_root(self) -> Path:
        """Return the configured frontend directory.

        A Bun build can either write directly into ``web/`` (the repository's
        default) or into a conventional ``web/dist`` directory.  Prefer a
        build directory when it contains an index page, while retaining the
        checked-in page as a zero-dependency fallback for CLI users.
        """
        configured = getattr(self.server, "web_dir", None)
        candidates = [Path(configured)] if configured else []
        root = self.store.root_dir
        candidates.extend((root / "web" / "dist", root / "dist", root / "web"))
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if (candidate / "index.html").is_file():
                return candidate
        return (root / "web").resolve()

    def _serve_static(self, request_path: str) -> bool:
        """Serve a frontend asset, returning whether a response was sent.

        Static requests are kept separate from ``/api`` so adding an asset can
        never expose files outside the selected frontend directory.  Unknown
        extensionless paths fall back to ``index.html`` for Bun/SPA routes.
        """
        root = self._static_root()
        root = root.resolve()
        relative = unquote(request_path).lstrip("/")
        # Reject traversal before resolving the candidate.  The containment
        # check below also covers encoded traversal and symlink escapes.
        try:
            relative_parts = Path(relative).parts
        except (OSError, ValueError):
            self._send_json(404, {"error": "not found"})
            return True
        if ".." in relative_parts:
            self._send_json(404, {"error": "not found"})
            return True
        try:
            candidate = (root / relative).resolve()
        except (OSError, ValueError):
            self._send_json(404, {"error": "not found"})
            return True
        if candidate != root and root not in candidate.parents:
            self._send_json(404, {"error": "not found"})
            return True
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            # A frontend router may own extensionless paths such as /settings.
            # Do not make arbitrary .js/.css typos appear to succeed.
            if "." in Path(relative).name:
                self._send_json(404, {"error": "not found"})
                return True
            candidate = root / "index.html"
            if not candidate.is_file():
                if request_path.rstrip("/") == "":
                    return False
                self._send_json(404, {"error": "not found"})
                return True
        try:
            body = candidate.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return True
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self._send_bytes(200, body, content_type)
        return True

    def _body_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1_000_000:
                raise CertificateError("request body is too large")
            value = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(value, dict):
                raise CertificateError("request body must be a JSON object")
            return value
        except json.JSONDecodeError as exc:
            raise CertificateError(f"invalid JSON: {exc.msg}")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/":
            if not self._serve_static("/"):
                self._send_json(200, {"name": "Corn Certificate Manager", "api": "/api/health"})
            return
        parts = path.strip("/").split("/")
        if parts[:1] == ["api"]:
            if len(parts) >= 2 and parts[1] == "v1":
                parts = ["api", *parts[2:]]
            try:
                self._get_api(parts)
            except Exception as exc:
                self._error(exc)
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:
        """Support health checks and asset probes without sending a body."""
        # Reuse GET routing while suppressing response bodies.  This keeps
        # Content-Type/Length and API status behavior identical to GET.
        self._head_only = True
        try:
            self.do_GET()
        finally:
            del self._head_only

    def _get_api(self, parts: list[str]) -> None:
        if parts == ["api", "health"]:
            status = self.store.ca_status()
            status.update({"status": "ok", "certificates": len(self.store.list_certificates())})
            self._send_json(200, status)
            return
        if parts in (["api", "ca"], ["api", "roots"]):
            self._send_json(200, self.store.ca_status())
            return
        if parts == ["api", "registrations"]:
            values = self.store.list_registrations()
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) == 2 and parts[0] == "api" and parts[1] == "certificates":
            query = parse_qs(urlsplit(self.path).query)
            values = self.store.list_certificates((query.get("type") or [None])[0])
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "certificates":
            cert_id = int(parts[2])
            if len(parts) == 3:
                value = self.store.get_certificate(cert_id)
                if value is None:
                    self._send_json(404, {"error": "certificate not found"})
                else:
                    self._send_json(200, value)
                return
            if len(parts) == 4 and parts[3] == "download":
                query = parse_qs(urlsplit(self.path).query)
                requested_format = (query.get("format") or [""])[0].lower()
                if requested_format:
                    if requested_format not in {"crt", "key", "p12", "pem", "bundle"}:
                        raise CertificateError("format must be crt, key, p12, pem or bundle")
                    record = self.store.get_certificate(cert_id)
                    if not record:
                        self._send_json(404, {"error": "certificate not found"})
                        return
                    suffix = ".bundle.crt" if requested_format == "bundle" else f".{requested_format}"
                    candidates = [name for name in record["files"] if name.endswith(suffix) and name.startswith("rsa_")]
                    if not candidates:
                        candidates = [name for name in record["files"] if name.endswith(suffix)]
                    if not candidates:
                        raise FileNotFoundError(requested_format)
                    path, filename = self.store.artifact(cert_id, candidates[0])
                    self._send_bytes(200, path.read_bytes(), mimetypes.guess_type(filename)[0] or "application/octet-stream", filename)
                    return
                body, filename = self.store.archive(cert_id)
                self._send_bytes(200, body, "application/zip", filename)
                return
            if len(parts) == 5 and parts[3] == "files":
                path, filename = self.store.artifact(cert_id, parts[4])
                self._send_bytes(200, path.read_bytes(), mimetypes.guess_type(filename)[0] or "application/octet-stream", filename)
                return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        if parts[:1] == ["api"] and len(parts) >= 2 and parts[1] == "v1":
            parts = ["api", *parts[2:]]
        try:
            body = self._body_json()
            if parts == ["api", "ca", "initialize"]:
                rootpass = body.get("rootpass") or os.environ.get("ROOTPASS") or os.environ.get("rootpass")
                result = self.store.initialize_ca(rootpass, bool(body.get("force", False)))
                self._send_json(200, result)
                return
            if parts == ["api", "registrations"]:
                record = self.store.create_registration(body)
                self._send_json(201, record)
                return
            if parts == ["api", "certificates"]:
                record = self.store.issue(body, body.get("rootpass"))
                self._send_json(201, record)
                return
            self._send_json(404, {"error": "not found"})
        except Exception as exc:
            self._error(exc)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, KeyError) or isinstance(exc, FileNotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(exc, CertificateError):
            status = getattr(exc, "status", HTTPStatus.BAD_REQUEST)
        elif isinstance(exc, ValueError):
            status = HTTPStatus.BAD_REQUEST
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(int(status), {"error": str(exc) or status.phrase})

    def log_message(self, format: str, *args: Any) -> None:
        # Keep normal HTTP access logs useful while avoiding noisy stack traces.
        super().log_message(format, *args)


def create_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    root_dir: Path = ROOT_DIR,
    db_path: Path = DB_PATH,
    web_dir: Optional[Path] = None,
) -> ThreadingHTTPServer:
    store = CertificateStore(root_dir, db_path)
    httpd = ThreadingHTTPServer((host, port), APIHandler)
    httpd.store = store  # type: ignore[attr-defined]
    if web_dir:
        configured_web_dir = Path(web_dir)
        if not configured_web_dir.is_absolute():
            configured_web_dir = Path(root_dir) / configured_web_dir
        httpd.web_dir = configured_web_dir.resolve()  # type: ignore[attr-defined]
    else:
        httpd.web_dir = None  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="CornTech certificate management API")
    parser.add_argument("--host", default=os.environ.get("CERT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CERT_PORT", "8080")))
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("CERT_DB", str(DB_PATH))))
    parser.add_argument(
        "--web-dir",
        type=Path,
        default=Path(os.environ["CERT_WEB_DIR"]) if os.environ.get("CERT_WEB_DIR") else None,
        help="frontend directory (defaults to web/dist, dist, then web)",
    )
    args = parser.parse_args()
    httpd = create_server(args.host, args.port, ROOT_DIR, args.db, args.web_dir)
    print(f"Corn certificate API listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.store.close()  # type: ignore[attr-defined]
        httpd.server_close()


if __name__ == "__main__":
    main()
