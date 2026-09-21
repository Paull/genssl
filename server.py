#!/usr/bin/env python3
"""Single-machine HTTP API for CornTech self-signed certificates.

The server deliberately delegates certificate issuance to the existing shell
scripts so CLI and HTTP generated artifacts stay compatible.  It only adds a
small SQLite catalog and a read/download API around the files in ``out/``.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hmac
import hashlib
import secrets
import time
from contextlib import contextmanager
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

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None
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
        # A lock file extends the in-process lock to CLI/API processes sharing
        # this single-machine CA.  OpenSSL's serial and index files are not
        # safe to update concurrently.
        self.lock_path = self.out_dir / ".ca-issue.lock"
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Versioned, transactional migration; v2 rows retain IDs and files."""
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            now = _utc_now()
            self.db.execute("INSERT OR IGNORE INTO agents(name,status,created_at,updated_at) VALUES('platform','active',?,?)", (now, now))
            platform = int(self.db.execute("SELECT id FROM agents WHERE name='platform'").fetchone()[0])
            self.db.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','agent')),
                agent_id INTEGER REFERENCES agents(id),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK((role='admin' AND agent_id IS NULL) OR (role='agent' AND agent_id IS NOT NULL)))""")
            schemas = {
                'registrations': """id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    agent_id INTEGER NOT NULL REFERENCES agents(id), created_by INTEGER REFERENCES users(id)""",
                'certificates': """id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cert_type TEXT NOT NULL CHECK(cert_type IN ('client','server')), name TEXT NOT NULL,
                    sans_json TEXT NOT NULL DEFAULT '[]', days INTEGER NOT NULL, output_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'issued',
                    agent_id INTEGER NOT NULL REFERENCES agents(id), created_by INTEGER REFERENCES users(id),
                    registration_id INTEGER REFERENCES registrations(id), metadata_json TEXT NOT NULL DEFAULT '{}',
                    serial TEXT, not_before TEXT, not_after TEXT""",
            }
            for table, schema in schemas.items():
                exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not exists:
                    self.db.execute(f"CREATE TABLE {table} ({schema})")
                    continue
                columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                # Rebuild older tables once to enforce NOT NULL and FK rules,
                # copying only existing columns and assigning historical rows.
                required = {'agent_id','created_by'} | ({'metadata_json','serial','not_before','not_after','registration_id'} if table == 'certificates' else set())
                if not required.issubset(columns) or not self.db.execute(f"PRAGMA foreign_key_list({table})").fetchone():
                    self.db.execute(f"CREATE TABLE {table}_v3 ({schema})")
                    target_cols = [r[1] for r in self.db.execute(f"PRAGMA table_info({table}_v3)")]
                    copied = target_cols
                    expressions = []
                    for c in target_cols:
                        if c == 'agent_id':
                            expressions.append(f'COALESCE(agent_id,{platform})' if c in columns else str(platform))
                        elif c in columns:
                            expressions.append(c)
                        elif c == 'metadata_json':
                            expressions.append("'{}'")
                        elif c == 'sans_json':
                            expressions.append("'[]'")
                        elif c == 'status':
                            expressions.append("'issued'")
                        else:
                            expressions.append('NULL')
                    self.db.execute(f"INSERT INTO {table}_v3 ({','.join(copied)}) SELECT {','.join(expressions)} FROM {table}")
                    self.db.execute(f"DROP TABLE {table}")
                    self.db.execute(f"ALTER TABLE {table}_v3 RENAME TO {table}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER REFERENCES users(id),
                actor_role TEXT, action TEXT NOT NULL, resource_type TEXT, resource_id TEXT,
                agent_id INTEGER REFERENCES agents(id), details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
            for table, field in [('certificates','name'),('certificates','agent_id'),('registrations','name'),('registrations','agent_id'),('users','agent_id')]:
                self.db.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{field} ON {table}({field})")
            # SQL-level protection also covers trusted local migration writers.
            for event in ('INSERT', 'UPDATE'):
                self.db.execute(f"""CREATE TRIGGER IF NOT EXISTS certificate_registration_owner_{event.lower()}
                    BEFORE {event} ON certificates WHEN NEW.registration_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM registrations WHERE id=NEW.registration_id AND agent_id=NEW.agent_id)
                    BEGIN SELECT RAISE(ABORT, 'registration ownership mismatch'); END""")
            self.db.execute("PRAGMA user_version=3")

    @staticmethod
    def hash_password(password: str) -> str:
        if not isinstance(password, str) or len(password) < 8:
            raise CertificateError("password must be at least 8 characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180000)
        return "pbkdf2_sha256$180000$%s$%s" % (base64.urlsafe_b64encode(salt).decode(), base64.urlsafe_b64encode(digest).decode())

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            scheme, iterations, salt, digest = encoded.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            raw_salt = base64.urlsafe_b64decode(salt.encode())
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), raw_salt, int(iterations))
            return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode(), digest)
        except (ValueError, TypeError):
            return False

    @contextmanager
    def ca_lock(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with self.issue_lock:
            handle = self.lock_path.open("a+")
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def close(self) -> None:
        self.db.close()

    @property
    def platform_agent_id(self) -> int:
        with self.db_lock:
            row = self.db.execute("SELECT id FROM agents WHERE name='platform'").fetchone()
        return int(row[0])

    def _agent_json(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        with self.db_lock:
            count = self.db.execute("SELECT COUNT(*) FROM users WHERE agent_id=?", (value["id"],)).fetchone()[0]
            certs = self.db.execute("SELECT COUNT(*) FROM certificates WHERE agent_id=?", (value["id"],)).fetchone()[0]
        value.update({"users": int(count), "certificates": int(certs)})
        return value

    def list_agents(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM agents"
        if not include_disabled:
            query += " WHERE status='active'"
        query += " ORDER BY id"
        with self.db_lock:
            rows = self.db.execute(query).fetchall()
        return [self._agent_json(r) for r in rows]

    def get_agent(self, agent_id: int) -> Optional[dict[str, Any]]:
        with self.db_lock:
            row = self.db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return self._agent_json(row) if row else None

    def create_agent(self, name: str) -> dict[str, Any]:
        name = self._validate_name(name, "agent name")
        if name == "platform":
            raise CertificateError("platform is reserved")
        now = _utc_now()
        try:
            with self.db_lock, self.db:
                cur = self.db.execute("INSERT INTO agents(name,status,created_at,updated_at) VALUES(?, 'active', ?, ?)", (name, now, now))
                agent_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            err = CertificateError("agent already exists")
            err.status = HTTPStatus.CONFLICT
            raise err
        return self.get_agent(agent_id) or {}

    def update_agent(self, agent_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get_agent(agent_id)
        if not current:
            raise KeyError(agent_id)
        if current["name"] == "platform" and payload.get("status") == "disabled":
            raise CertificateError("platform agent cannot be disabled")
        if current["name"] == "platform" and payload.get("name") not in (None, "platform"):
            raise CertificateError("platform agent cannot be renamed")
        fields, params = [], []
        if "name" in payload:
            fields.append("name=?"); params.append(self._validate_name(payload["name"], "agent name"))
        if "status" in payload:
            if payload["status"] not in ("active", "disabled"):
                raise CertificateError("status must be active or disabled")
            fields.append("status=?"); params.append(payload["status"])
        if fields:
            fields.append("updated_at=?"); params.append(_utc_now()); params.append(agent_id)
            with self.db_lock, self.db:
                self.db.execute(f"UPDATE agents SET {', '.join(fields)} WHERE id=?", params)
            if payload.get("status") == "disabled":
                with self.db_lock, self.db:
                    self.db.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE agent_id=?)", (agent_id,))
        return self.get_agent(agent_id) or {}

    def user_json(self, row: sqlite3.Row | dict[str, Any], include_secret: bool = False) -> dict[str, Any]:
        value = dict(row)
        value.pop("password_hash", None)
        if value.get("agent_id"):
            agent = self.get_agent(int(value["agent_id"]))
            value["agent"] = agent
        return value

    def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        with self.db_lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self.user_json(row) if row else None

    def find_user(self, username: str) -> Optional[sqlite3.Row]:
        with self.db_lock:
            return self.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def list_users(self, agent_id: Optional[int] = None) -> list[dict[str, Any]]:
        with self.db_lock:
            if agent_id is None:
                rows = self.db.execute("SELECT * FROM users ORDER BY id").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM users WHERE agent_id=? ORDER BY id", (agent_id,)).fetchall()
        return [self.user_json(r) for r in rows]

    def create_user(self, username: str, password: str, role: str = "agent", agent_id: Optional[int] = None) -> dict[str, Any]:
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", username):
            raise CertificateError("username must contain 1-128 letters, digits, dot, underscore, @ or hyphen")
        if role not in ("admin", "agent"):
            raise CertificateError("role must be admin or agent")
        if role == "agent":
            agent = self.get_agent(int(agent_id)) if agent_id is not None else None
            if agent is None or agent.get("status") != "active":
                raise CertificateError("agent_id is required for agent users")
        else:
            agent_id = None
        encoded = self.hash_password(password)
        now = _utc_now()
        try:
            with self.db_lock, self.db:
                cur = self.db.execute("INSERT INTO users(username,password_hash,role,agent_id,status,created_at,updated_at) VALUES(?,?,?,?,'active',?,?)", (username, encoded, role, agent_id, now, now))
                user_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            err = CertificateError("username already exists")
            err.status = HTTPStatus.CONFLICT
            raise err
        return self.get_user(user_id) or {}

    def ensure_bootstrap_user(self, username: Optional[str], password: Optional[str]) -> None:
        """Mirror WEB_USERNAME/WEB_PASSWORD into a hashed admin account.

        Basic Auth remains accepted for old clients, while the account also
        enables Bearer login without persisting the cleartext password.
        """
        if not username or not password:
            return
        row = self.find_user(username)
        if row is None:
            try:
                self.create_user(username, password, "admin")
            except CertificateError:
                pass

    def update_user(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db_lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError(user_id)
        fields, params = [], []
        if "password" in payload:
            fields.append("password_hash=?"); params.append(self.hash_password(payload["password"]))
        if "status" in payload:
            if payload["status"] not in ("active", "disabled"):
                raise CertificateError("status must be active or disabled")
            if payload["status"] == "disabled" and row["role"] == "admin":
                with self.db_lock:
                    active_admins = self.db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]
                if int(active_admins) <= 1:
                    raise CertificateError("at least one active administrator is required")
            fields.append("status=?"); params.append(payload["status"])
        if fields:
            fields.append("updated_at=?"); params.append(_utc_now()); params.append(user_id)
            with self.db_lock, self.db:
                self.db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", params)
            with self.db_lock, self.db:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return self.get_user(user_id) or {}

    def create_session(self, user_id: int, ttl: int = 86400) -> tuple[str, str]:
        token = secrets.token_urlsafe(36)
        now = _utc_now()
        expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ttl)).replace(microsecond=0).isoformat()
        with self.db_lock, self.db:
            self.db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, expires, now))
        return token, expires

    def actor_for_token(self, token: str) -> Optional[dict[str, Any]]:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db_lock:
            row = self.db.execute("SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (digest,)).fetchone()
        if not row or row["status"] != "active":
            return None
        if row["role"] == "agent" and row["agent_id"]:
            with self.db_lock:
                agent = self.db.execute("SELECT status FROM agents WHERE id=?", (row["agent_id"],)).fetchone()
            if not agent or agent["status"] != "active":
                return None
        try:
            if _dt.datetime.fromisoformat(row["expires_at"]) <= _dt.datetime.now(_dt.timezone.utc):
                return None
        except ValueError:
            return None
        value = dict(row)
        value["user_id"] = int(value.pop("id"))
        value.pop("password_hash", None)
        return value

    def audit(self, actor: Optional[dict[str, Any]], action: str, resource_type: str = "", resource_id: Any = None, agent_id: Optional[int] = None, details: Optional[dict[str, Any]] = None) -> None:
        with self.db_lock, self.db:
            self.db.execute("INSERT INTO audit_events(actor_user_id,actor_role,action,resource_type,resource_id,agent_id,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (actor.get("user_id") if actor else None, actor.get("role") if actor else "anonymous", action, resource_type, str(resource_id) if resource_id is not None else None, agent_id, json.dumps(details or {}, ensure_ascii=False), _utc_now()))

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db_lock:
            rows = self.db.execute("SELECT id,actor_user_id,actor_role,action,resource_type,resource_id,agent_id,details_json,created_at FROM audit_events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
        values = []
        for row in rows:
            value = dict(row); value["details"] = json.loads(value.pop("details_json") or "{}")
            values.append(value)
        return values

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

    def list_certificates(self, cert_type: Optional[str] = None, agent_id: Optional[int] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM certificates"
        params: tuple[Any, ...] = ()
        clauses = []
        if cert_type:
            clauses.append("cert_type = ?")
            params += (cert_type,)
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params += (agent_id,)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC"
        with self.db_lock:
            rows = self.db.execute(query, params).fetchall()
        return [self._row_json(r) for r in rows]

    def get_certificate(self, cert_id: int, agent_id: Optional[int] = None) -> Optional[dict[str, Any]]:
        with self.db_lock:
            if agent_id is None:
                row = self.db.execute("SELECT * FROM certificates WHERE id = ?", (cert_id,)).fetchone()
            else:
                row = self.db.execute("SELECT * FROM certificates WHERE id = ? AND agent_id = ?", (cert_id, agent_id)).fetchone()
        return self._row_json(row) if row else None

    def get_registration(self, registration_id: int, agent_id: Optional[int] = None) -> Optional[dict[str, Any]]:
        with self.db_lock:
            if agent_id is None:
                row = self.db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
            else:
                row = self.db.execute("SELECT * FROM registrations WHERE id=? AND agent_id=?", (registration_id, agent_id)).fetchone()
        return dict(row) if row else None

    def list_registrations(self, agent_id: Optional[int] = None) -> list[dict[str, Any]]:
        with self.db_lock:
            if agent_id is None:
                rows = self.db.execute("SELECT * FROM registrations ORDER BY id DESC").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM registrations WHERE agent_id=? ORDER BY id DESC", (agent_id,)).fetchall()
        return [dict(row) for row in rows]

    def create_registration(self, payload: dict[str, Any], agent_id: Optional[int] = None) -> dict[str, Any]:
        name = payload.get("name") or payload.get("app")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
            raise CertificateError("registration name must be 1-128 characters")
        owner = payload.get("owner", "")
        notes = payload.get("notes", "")
        if not isinstance(owner, str) or not isinstance(notes, str):
            raise CertificateError("owner and notes must be strings")
        agent_id = agent_id or self.platform_agent_id
        with self.db_lock, self.db:
            cur = self.db.execute(
                "INSERT INTO registrations(name,owner,notes,created_at,agent_id) VALUES(?,?,?,?,?)",
                (name.strip(), owner[:256], notes[:2000], _utc_now(), agent_id),
            )
            registration_id = int(cur.lastrowid)
            row = self.db.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,)).fetchone()
        return dict(row) if row else {}

    def files_for_dir(self, relative_dir: str) -> list[str]:
        directory = self._safe_output_dir(relative_dir)
        if not directory.is_dir():
            return []
        # Legacy CLI folders contain links to their timestamped files.  Allow
        # links only when they resolve inside this certificate directory;
        # links to shared roots or another agent remain hidden.
        return sorted(p.name for p in directory.iterdir() if p.is_file() and (p.resolve() == directory or directory in p.resolve().parents))

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

    def issue(self, payload: dict[str, Any], rootpass: Optional[str] = None, actor: Optional[dict[str, Any]] = None, agent_id: Optional[int] = None) -> dict[str, Any]:
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
        registration_id = payload.get("registration_id")
        if registration_id is not None:
            try:
                registration_id = int(registration_id)
            except (TypeError, ValueError):
                raise CertificateError("registration_id must be an integer")
            if not self.get_registration(registration_id, agent_id or self.platform_agent_id):
                raise CertificateError("registration does not belong to this agent")
        if not self._root_available():
            err = CertificateError("CA root certificates are missing; initialize /api/ca first")
            err.status = HTTPStatus.CONFLICT
            raise err
        # Agent requests use the server's CA credential and cannot submit it
        # (or the destructive force flag) in their request body.
        if actor and actor.get("role") == "agent" and ("rootpass" in payload or "force" in payload or rootpass is not None):
            raise CertificateError("agent certificate requests cannot contain rootpass or force")
        env = os.environ.copy()
        secret = rootpass if rootpass is not None else (env.get("ROOTPASS") or env.get("rootpass"))
        if not secret:
            err = CertificateError("ROOTPASS environment variable is required")
            err.status = HTTPStatus.CONFLICT
            raise err
        env["rootpass"] = secret
        env["days"] = str(days)
        script = self.root_dir / ("gen_client_cert.sh" if cert_type == "client" else "gen_server_cert.sh")
        output_base = self.out_dir / "agents" / str(agent_id or self.platform_agent_id) / cert_type / uuid.uuid4().hex
        output_base.mkdir(parents=True, exist_ok=True)
        env["CERT_OUTPUT_BASE"] = str(output_base)
        script = self.root_dir / "scripts" / ("openssl-client.sh" if cert_type == "client" else "openssl-server.sh")
        if not script.is_file():
            script = self.root_dir / ("gen_client_cert.sh" if cert_type == "client" else "gen_server_cert.sh")
        args = ["bash", str(script), name] if cert_type == "client" else ["bash", str(script), *sans]
        with self.ca_lock():
            before = set(output_base.glob("*") if output_base.is_dir() else ())
            proc = subprocess.run(args, cwd=self.root_dir, env=env, text=True, capture_output=True, timeout=180)
            if proc.returncode != 0:
                raise CertificateError(f"certificate script failed: {proc.stderr[-800:] or proc.stdout[-800:]}")
            generated = self._find_generated_dir(name, before, output_base)
            if generated is None:
                raise CertificateError("certificate script completed without output files")
            expected = (f"rsa_{name}.crt", f"rsa_{name}.key", f"ec_{name}.crt", f"ec_{name}.key")
            missing = [filename for filename in expected if not (generated / filename).is_file()]
            if missing:
                raise CertificateError(f"certificate script did not create required files: {', '.join(missing)}")
            # Keep an immutable, uniquely named API artifact even though the
            # legacy scripts group output by minute and may reuse that folder.
            api_dir = generated
            if key_type != "both":
                unwanted = "ec_" if key_type == "rsa" else "rsa_"
                for artifact in api_dir.iterdir():
                    if artifact.name.startswith(unwanted):
                        artifact.unlink()
        relative = str(api_dir.relative_to(self.root_dir))
        serial = not_before = not_after = None
        cert_probe = api_dir / f"rsa_{name}.crt"
        if cert_probe.is_file():
            try:
                values = subprocess.check_output(["openssl", "x509", "-in", str(cert_probe), "-noout", "-serial", "-startdate", "-enddate"], text=True, timeout=10).splitlines()
                serial = next((line.split("=", 1)[1].strip() for line in values if line.startswith("serial=")), None)
                not_before = next((line.split("=", 1)[1].strip() for line in values if line.startswith("notBefore=")), None)
                not_after = next((line.split("=", 1)[1].strip() for line in values if line.startswith("notAfter=")), None)
            except (OSError, subprocess.SubprocessError):
                pass
        with self.db_lock, self.db:
            cur = self.db.execute(
                "INSERT INTO certificates(cert_type,name,sans_json,days,output_dir,created_at,status,agent_id,created_by,registration_id,serial,not_before,not_after) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cert_type, name, json.dumps(sans), days, relative, _utc_now(), "issued", agent_id or self.platform_agent_id, actor.get("user_id") if actor else None, registration_id, serial, not_before, not_after),
            )
            cert_id = int(cur.lastrowid)
        return self.get_certificate(cert_id) or {}

    def _find_generated_dir(self, name: str, before: set[Path], base: Optional[Path] = None) -> Optional[Path]:
        base = base or (self.out_dir / name)
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
        with self.ca_lock():
            self._ensure_ca_state()
            if self._root_available():
                if force:
                    err = CertificateError("CA already initialized; destructive force reset is disabled")
                    err.status = HTTPStatus.CONFLICT
                    raise err
                return self.ca_status()
            existing_material = [self.out_dir / f for f in ("rsa_root.crt", "rsa_root.key", "ec_root.crt", "ec_root.key") if (self.out_dir / f).exists()]
            if existing_material:
                err = CertificateError("partial CA material exists; restore a complete backup before initialization")
                err.status = HTTPStatus.CONFLICT
                raise err
            if force or not self._root_available():
                for filename in ("rsa_root.crt", "rsa_root.key", "ec_root.crt", "ec_root.key"):
                    (self.out_dir / filename).unlink(missing_ok=True)
            env = os.environ.copy()
            env["rootpass"] = rootpass
            root_script = self.root_dir / "scripts" / "openssl-root.sh"
            if not root_script.is_file():
                root_script = self.root_dir / "gen_root_cert.sh"
            proc = subprocess.run(["bash", str(root_script)], cwd=self.root_dir, env=env, text=True, capture_output=True, timeout=180)
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
        if directory.resolve() not in path.parents or not path.is_file():
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
                target = item.resolve()
                if item.is_file() and directory.resolve() in target.parents:
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

    def _send_auth_required(self) -> None:
        body = _json_bytes({"error": "authentication required"})
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="CornTech Certificate Manager", charset="UTF-8", Bearer')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _authenticate(self) -> Optional[dict[str, Any]]:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            actor = self.store.actor_for_token(authorization[7:].strip())
            if actor:
                return actor
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                username, separator, password = decoded.partition(":")
                if separator:
                    configured_username = getattr(self.server, "web_username", None)
                    configured_password = getattr(self.server, "web_password", None)
                    row = self.store.find_user(username)
                    agent_active = True
                    if row and row["role"] == "agent" and row["agent_id"]:
                        agent = self.store.get_agent(int(row["agent_id"]))
                        agent_active = bool(agent and agent.get("status") == "active")
                    if row and row["status"] == "active" and agent_active and self.store.verify_password(password, row["password_hash"]):
                        value = dict(row); value["user_id"] = int(value.pop("id")); value.pop("password_hash", None)
                        return value
                    # Configured Basic bootstrap is a first-run compatibility
                    # path only; once the username exists, disabled/rotated
                    # persisted state wins and wrong credentials fail closed.
                    if row is None and configured_username and hmac.compare_digest(username, configured_username) and hmac.compare_digest(password, configured_password or ""):
                        return {"user_id": None, "username": username, "role": "admin", "agent_id": None, "status": "active", "bootstrap": True}
            except (ValueError, UnicodeDecodeError):
                pass
        return None

    def _require_auth(self) -> bool:
        actor = self._authenticate()
        if actor is None:
            # Existing fixture and local CLI integrations intentionally run
            # without configured credentials.  Once a bootstrap or user is
            # configured, every protected route requires an identity.
            if not getattr(self.server, "auth_enabled", False) or os.environ.get("CERT_ALLOW_ANONYMOUS") == "1":
                actor = {"user_id": None, "username": "anonymous", "role": "admin", "agent_id": None, "status": "active", "anonymous": True}
            else:
                self._send_auth_required()
                return False
        self.actor = actor
        return True

    def _actor(self) -> dict[str, Any]:
        return getattr(self, "actor", {"user_id": None, "username": "anonymous", "role": "admin", "agent_id": None})

    def _is_admin(self) -> bool:
        return self._actor().get("role") == "admin"

    def _agent_scope(self) -> Optional[int]:
        actor = self._actor()
        return None if actor.get("role") == "admin" else int(actor.get("agent_id"))

    def _validate_agent_target(self, agent_id: Optional[int], *, allow_platform: bool = True) -> int:
        if agent_id is None:
            return self.store.platform_agent_id
        record = self.store.get_agent(int(agent_id))
        if not record or record.get("status") != "active":
            raise CertificateError("agent_id is missing or disabled")
        if not allow_platform and record.get("name") == "platform":
            raise CertificateError("platform ownership is reserved")
        return int(agent_id)

    def _require_admin(self) -> bool:
        if self._is_admin():
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "administrator role required"})
        return False

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path.startswith("/api/") and path != "/api/health" and not self._require_auth():
            return
        if path == "/api/health" and not hasattr(self, "actor"):
            self.actor = {"user_id": None, "username": "anonymous", "role": "admin", "agent_id": None}
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
            self._send_json(200, {"status": "ok", "version": self.server_version})
            return
        if parts in (["api", "auth", "me"], ["api", "me"]):
            actor = self._actor().copy()
            actor.pop("password_hash", None)
            self._send_json(200, {"enabled": True, "user": actor, **actor})
            return
        if parts == ["api", "agents"]:
            if not self._require_admin(): return
            values = self.store.list_agents()
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "users":
            if not self._require_admin(): return
            values = self.store.list_users(int(parts[2]))
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) == 3 and parts[1] == "agents":
            agent_id = int(parts[2]); record = self.store.get_agent(agent_id)
            if not record: self._send_json(404, {"error": "agent not found"})
            elif self._is_admin() or self._agent_scope() == agent_id: self._send_json(200, record)
            else: self._send_json(403, {"error": "agent ownership required"})
            return
        if parts == ["api", "users"]:
            if not self._require_admin(): return
            values = self.store.list_users()
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) == 3 and parts[1] == "users":
            if not self._require_admin(): return
            value = self.store.get_user(int(parts[2]))
            self._send_json(200 if value else 404, value or {"error": "user not found"})
            return
        if parts == ["api", "audit"]:
            if not self._require_admin(): return
            self._send_json(200, {"items": self.store.list_audit(), "total": len(self.store.list_audit())})
            return
        if parts in (["api", "ca"], ["api", "roots"]):
            self._send_json(200, self.store.ca_status())
            return
        if parts == ["api", "registrations"]:
            query = parse_qs(urlsplit(self.path).query)
            requested_agent = (query.get("agent_id") or [None])[0]
            filter_agent = self._agent_scope()
            if requested_agent is not None:
                if not self._is_admin() and int(requested_agent) != filter_agent:
                    self._send_json(403, {"error": "agent ownership required"}); return
                filter_agent = self._validate_agent_target(int(requested_agent))
            values = self.store.list_registrations(filter_agent)
            self.store.audit(self._actor(), "registration.list", "registration", agent_id=self._agent_scope())
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) == 3 and parts[:2] == ["api", "registrations"]:
            value = self.store.get_registration(int(parts[2]), self._agent_scope())
            self._send_json(200 if value else 404, value or {"error": "registration not found"})
            return
        if len(parts) == 2 and parts[0] == "api" and parts[1] == "certificates":
            query = parse_qs(urlsplit(self.path).query)
            requested_agent = (query.get("agent_id") or [None])[0]
            filter_agent = self._agent_scope()
            if requested_agent is not None:
                if not self._is_admin() and int(requested_agent) != filter_agent:
                    self._send_json(403, {"error": "agent ownership required"}); return
                filter_agent = self._validate_agent_target(int(requested_agent))
            values = self.store.list_certificates((query.get("type") or [None])[0], filter_agent)
            self.store.audit(self._actor(), "certificate.list", "certificate", agent_id=filter_agent, details={"type": (query.get("type") or [None])[0]})
            self._send_json(200, {"items": values, "total": len(values)})
            return
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "certificates":
            cert_id = int(parts[2])
            if len(parts) == 3:
                value = self.store.get_certificate(cert_id, self._agent_scope())
                if value is None:
                    self._send_json(404, {"error": "certificate not found"})
                else:
                    self.store.audit(self._actor(), "certificate.detail", "certificate", cert_id, value.get("agent_id"))
                    self._send_json(200, value)
                return
            if len(parts) == 4 and parts[3] == "download":
                query = parse_qs(urlsplit(self.path).query)
                requested_format = (query.get("format") or [""])[0].lower()
                if requested_format:
                    if requested_format not in {"crt", "key", "p12", "pem", "bundle"}:
                        raise CertificateError("format must be crt, key, p12, pem or bundle")
                    record = self.store.get_certificate(cert_id, self._agent_scope())
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
                    self.store.audit(self._actor(), "certificate.download", "certificate", cert_id, record.get("agent_id"), {"filename": filename, "format": requested_format})
                    self._send_bytes(200, path.read_bytes(), mimetypes.guess_type(filename)[0] or "application/octet-stream", filename)
                    return
                # archive/artifact perform a second ownership check below;
                # the scoped lookup above prevents ID probing across agents.
                if self.store.get_certificate(cert_id, self._agent_scope()) is None:
                    self._send_json(404, {"error": "certificate not found"}); return
                body, filename = self.store.archive(cert_id)
                record = self.store.get_certificate(cert_id, self._agent_scope())
                self.store.audit(self._actor(), "certificate.download", "certificate", cert_id, record.get("agent_id") if record else None, {"archive": True})
                self._send_bytes(200, body, "application/zip", filename)
                return
            if len(parts) == 5 and parts[3] == "files":
                if self.store.get_certificate(cert_id, self._agent_scope()) is None:
                    self._send_json(404, {"error": "certificate not found"}); return
                path, filename = self.store.artifact(cert_id, parts[4])
                record = self.store.get_certificate(cert_id, self._agent_scope())
                self.store.audit(self._actor(), "certificate.download", "certificate", cert_id, record.get("agent_id") if record else None, {"filename": filename})
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
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc and urlsplit(origin).netloc != self.headers.get("Host"):
                self._send_json(403, {"error": "cross-origin request rejected"}); return
            if parts == ["api", "auth", "login"]:
                username, password = body.get("username"), body.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise CertificateError("username and password are required")
                now_mono = time.monotonic()
                client_ip = self.client_address[0]
                with self.server.auth_attempt_lock:
                    attempts = [stamp for stamp in self.server.auth_attempts.get(client_ip, []) if now_mono - stamp < 60]
                    if len(attempts) >= 10:
                        self.server.auth_attempts[client_ip] = attempts
                        self._send_json(429, {"error": "too many authentication attempts"}); return
                    attempts.append(now_mono)
                    self.server.auth_attempts[client_ip] = attempts
                row = self.store.find_user(username)
                valid = bool(row and row["status"] == "active" and self.store.verify_password(password, row["password_hash"]))
                if valid and row["role"] == "agent":
                    agent = self.store.get_agent(int(row["agent_id"]))
                    valid = bool(agent and agent.get("status") == "active")
                configured = getattr(self.server, "web_username", None), getattr(self.server, "web_password", None)
                if not valid and row is None and configured[0] and hmac.compare_digest(username, configured[0]) and hmac.compare_digest(password, configured[1] or ""):
                    self.store.ensure_bootstrap_user(username, password)
                    row = self.store.find_user(username)
                    valid = bool(row)
                if not valid:
                    self._send_json(401, {"error": "invalid credentials"}); return
                with self.server.auth_attempt_lock:
                    self.server.auth_attempts.pop(client_ip, None)
                token, expires = self.store.create_session(int(row["id"]))
                actor = self.store.get_user(int(row["id"])) or {}
                self.store.audit(actor, "auth.login", "user", row["id"], row["agent_id"])
                self._send_json(200, {"token": token, "token_type": "Bearer", "expires_at": expires, "user": actor})
                return
            if not self._require_auth():
                return
            actor = self._actor()
            scope = self._agent_scope()
            if parts == ["api", "auth", "logout"]:
                authorization = self.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    digest = hashlib.sha256(authorization[7:].strip().encode()).hexdigest()
                    with self.store.db_lock, self.store.db:
                        self.store.db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
                self._send_json(200, {"ok": True}); return
            if parts == ["api", "agents"]:
                if not self._require_admin(): return
                record = self.store.create_agent(body.get("name"))
                self.store.audit(actor, "agent.create", "agent", record.get("id"), record.get("id"))
                self._send_json(201, record); return
            if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "users":
                if not self._require_admin(): return
                record = self.store.create_user(body.get("username"), body.get("password"), "agent", int(parts[2]))
                self.store.audit(actor, "user.create", "user", record.get("id"), int(parts[2]))
                self._send_json(201, record); return
            if parts == ["api", "users"]:
                if not self._require_admin(): return
                record = self.store.create_user(body.get("username"), body.get("password"), body.get("role", "agent"), body.get("agent_id"))
                self.store.audit(actor, "user.create", "user", record.get("id"), record.get("agent_id"))
                self._send_json(201, record); return
            if parts == ["api", "ca", "initialize"]:
                if not self._require_admin(): return
                rootpass = body.get("rootpass") or os.environ.get("ROOTPASS") or os.environ.get("rootpass")
                result = self.store.initialize_ca(rootpass, bool(body.get("force", False)))
                self.store.audit(actor, "ca.initialize", "ca", None)
                self._send_json(200, result)
                return
            if parts == ["api", "registrations"]:
                if scope is not None:
                    if body.get("agent_id") is not None and int(body["agent_id"]) != scope:
                        raise CertificateError("agent cannot override agent_id")
                    body.pop("agent_id", None)
                elif body.get("agent_id") is not None:
                    scope = int(body["agent_id"])
                scope = self._validate_agent_target(scope)
                record = self.store.create_registration(body, scope or self.store.platform_agent_id)
                self.store.audit(actor, "registration.create", "registration", record.get("id"), record.get("agent_id"))
                self._send_json(201, record)
                return
            if parts == ["api", "certificates"]:
                if scope is not None:
                    if body.get("agent_id") is not None and int(body["agent_id"]) != scope:
                        raise CertificateError("agent cannot override agent_id")
                    body.pop("agent_id", None)
                elif body.get("agent_id") is not None:
                    scope = int(body["agent_id"])
                scope = self._validate_agent_target(scope)
                record = self.store.issue(body, body.get("rootpass") if self._is_admin() else None, actor, scope or self.store.platform_agent_id)
                self.store.audit(actor, "certificate.issue", "certificate", record.get("id"), record.get("agent_id"), {"name": record.get("name"), "type": record.get("type")})
                self._send_json(201, record)
                return
            self._send_json(404, {"error": "not found"})
        except Exception as exc:
            self._error(exc)

    def do_PATCH(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        if parts[:1] == ["api"] and len(parts) >= 2 and parts[1] == "v1":
            parts = ["api", *parts[2:]]
        if not self._require_auth():
            return
        try:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc and urlsplit(origin).netloc != self.headers.get("Host"):
                self._send_json(403, {"error": "cross-origin request rejected"}); return
            body = self._body_json()
            if not self._require_admin():
                return
            if len(parts) == 3 and parts[1] == "agents":
                result = self.store.update_agent(int(parts[2]), body)
                self.store.audit(self._actor(), "agent.update", "agent", parts[2], int(parts[2]), {"fields": list(body)})
                self._send_json(200, result); return
            if len(parts) == 3 and parts[1] == "users":
                result = self.store.update_user(int(parts[2]), body)
                self.store.audit(self._actor(), "user.update", "user", parts[2])
                self._send_json(200, result); return
            self._send_json(404, {"error": "not found"})
        except Exception as exc:
            self._error(exc)

    def _error(self, exc: Exception) -> None:
        try:
            self.store.audit(getattr(self, "actor", None), "request.error", "http", details={"path": urlsplit(self.path).path, "error": str(exc)[:256]})
        except Exception:
            pass
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
    web_username: Optional[str] = None,
    web_password: Optional[str] = None,
) -> ThreadingHTTPServer:
    web_username = os.environ.get("WEB_USERNAME") if web_username is None else web_username
    web_password = os.environ.get("WEB_PASSWORD") if web_password is None else web_password
    if bool(web_username) != bool(web_password):
        raise ValueError("WEB_USERNAME and WEB_PASSWORD must be set together")
    store = CertificateStore(root_dir, db_path)
    store.ensure_bootstrap_user(web_username, web_password)
    httpd = ThreadingHTTPServer((host, port), APIHandler)
    httpd.store = store  # type: ignore[attr-defined]
    httpd.web_username = web_username or None  # type: ignore[attr-defined]
    httpd.web_password = web_password or None  # type: ignore[attr-defined]
    httpd.auth_attempts = {}  # type: ignore[attr-defined]
    httpd.auth_attempt_lock = threading.Lock()  # type: ignore[attr-defined]
    # With no configured credentials, retain the historical anonymous fixture
    # mode.  Any configured bootstrap or persisted user enables authorization.
    httpd.auth_enabled = os.environ.get("CERT_ALLOW_ANONYMOUS") != "1"  # type: ignore[attr-defined]
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
