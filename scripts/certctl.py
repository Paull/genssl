#!/usr/bin/env python3
"""Command line client for the single-machine CornTech certificate API.

The client deliberately has no third-party dependencies.  The v3 API
contract used here is:

* ``POST /api/auth/login`` with ``{"username", "password"}`` returns a
  bearer token (``token`` or ``access_token``) and, optionally, the user.
* ``GET /api/auth/me`` returns the authenticated user and role.
* Existing ``/api/certificates`` and ``/api/registrations`` paths are kept.
  An administrator may add ``agent_id`` to a request to choose ownership; an
  agent may only use its server-side default ownership.

``CERTCTL_TOKEN`` can be used for non-interactive automation.  Otherwise the
username is read from ``CERTCTL_USERNAME`` (or ``API_USERNAME``/
``WEB_USERNAME``) and the password from ``CERTCTL_PASSWORD`` (or
``API_PASSWORD``/
``WEB_PASSWORD``).  If no password is present and stdin is a terminal,
getpass is used.  Passwords and tokens are never included in normal output.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_NETWORK = 5
EXIT_API = 6
EXIT_FORBIDDEN = 7
EXIT_OUTPUT = 8

_SENSITIVE = re.compile(r"(?:token|password|passwd|secret|private[_-]?key|authorization)", re.I)
_ADMIN_ROLES = {"admin"}


class CLIError(Exception):
    """An expected command failure carrying the stable process exit code."""

    def __init__(self, message: str, code: int = EXIT_API):
        super().__init__(message)
        self.code = code


class HTTPFailure(CLIError):
    def __init__(self, status: int, message: str):
        code = {401: EXIT_AUTH, 403: EXIT_FORBIDDEN, 404: EXIT_NOT_FOUND}.get(status, EXIT_API)
        super().__init__(message, code)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A local API must never forward bearer credentials to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _public(value: Any) -> Any:
    """Remove credentials from API objects before displaying them."""
    if isinstance(value, Mapping):
        return {str(k): "[redacted]" if _SENSITIVE.search(str(k)) else _public(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return None


def _identity_from(value: Any) -> dict[str, Any]:
    """Normalize the few common login/me response envelopes."""
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("user")
    if not isinstance(nested, Mapping):
        nested = value.get("account") if isinstance(value.get("account"), Mapping) else {}
    identity = dict(nested)
    identity.update({k: v for k, v in value.items() if k not in {"token", "access_token", "accessToken", "user", "account"}})
    if "role" not in identity:
        role = _first(identity, "user_role", "userRole")
        if role:
            identity["role"] = role
    return identity


def _token_from(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    direct = _first(value, "token", "access_token", "accessToken", "id_token")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for key in ("data", "result", "auth"):
        nested = value.get(key)
        token = _token_from(nested)
        if token:
            return token
    return None


class APIClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip() if token else None
        self.username = username
        self.password = password
        self.timeout = timeout
        self.identity: dict[str, Any] | None = None
        self.session_path: pathlib.Path | None = None
        self.saved_token: str | None = None
        self.secrets: list[str] = []

    def remember_secret(self, value: str | None) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    @classmethod
    def from_environment(cls, args: argparse.Namespace) -> "APIClient":
        base_url = args.url or os.environ.get("CERTCTL_URL") or os.environ.get("API_URL") or "http://127.0.0.1:8080"
        token = os.environ.get("CERTCTL_TOKEN") or os.environ.get("API_TOKEN")
        username = args.auth_username or os.environ.get("CERTCTL_USERNAME") or os.environ.get("API_USERNAME") or os.environ.get("WEB_USERNAME")
        password = os.environ.get("CERTCTL_PASSWORD") or os.environ.get("API_PASSWORD") or os.environ.get("WEB_PASSWORD")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CLIError("API URL must be an http(s) URL without credentials, query or fragment", EXIT_USAGE)
        if args.timeout <= 0:
            raise CLIError("--timeout must be positive", EXIT_USAGE)
        client = cls(base_url, token=token, username=username, password=password, timeout=float(args.timeout))
        state_dir = pathlib.Path(os.environ.get("XDG_STATE_HOME", str(pathlib.Path.home() / ".local" / "state")))
        client.session_path = pathlib.Path(args.session_file or os.environ.get("CERTCTL_SESSION_FILE") or state_dir / "genssl" / "session.json").expanduser()
        # Explicit credentials always win over a previously cached identity.
        # Logout is the exception: invalidate the cached token even when a
        # username/password environment is still configured.
        if not token and args.command != "login" and (args.command == "logout" or not (username or password)):
            client.load_session()
        return client

    def load_session(self) -> None:
        if self.session_path is None:
            return
        try:
            info = self.session_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise CLIError("session file must be a regular file owned by you with mode 0600", EXIT_AUTH)
            fd = os.open(self.session_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError("session must be an object")
            if value.get("url") == self.base_url and isinstance(value.get("token"), str):
                self.token = self.saved_token = value["token"]
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise CLIError("cannot read session file; log in again", EXIT_AUTH) from exc

    def save_session(self) -> None:
        if self.session_path is None or not self.token:
            return
        temporary: str | None = None
        try:
            self.session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".certctl-", dir=self.session_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"url": self.base_url, "token": self.token}, stream)
            os.replace(temporary, self.session_path)
            self.saved_token = self.token
        except OSError as exc:
            raise CLIError("cannot save session file", EXIT_OUTPUT) from exc
        finally:
            if temporary:
                pathlib.Path(temporary).unlink(missing_ok=True)

    def clear_session(self) -> None:
        if self.session_path is not None:
            try:
                self.session_path.unlink(missing_ok=True)
            except OSError as exc:
                raise CLIError("cannot remove session file", EXIT_OUTPUT) from exc
        self.token = self.saved_token = None

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        values = [(str(k), str(v)) for k, v in (query or {}).items() if v is not None and v != ""]
        return url + ("?" + urllib.parse.urlencode(values) if values else "")

    def _headers(self, *, authenticated: bool = True, content_type: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = "application/json"
        if authenticated:
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        return headers

    @staticmethod
    def _decode_body(body: bytes, content_type: str = "") -> Any:
        if not body:
            return None
        if "json" in content_type.lower() or body[:1] in (b"{", b"["):
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return body

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        raw: bool = False,
    ) -> tuple[Any, Mapping[str, str]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self._url(path, query), data=body, method=method.upper(), headers=self._headers(authenticated=authenticated, content_type=body is not None))
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                result_body = response.read()
                headers = {k.lower(): v for k, v in response.headers.items()}
                result = result_body if raw else self._decode_body(result_body, headers.get("content-type", ""))
                if not raw and isinstance(result, bytes):
                    raise CLIError("API returned invalid JSON", EXIT_API)
                return result, headers
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            parsed = self._decode_body(error_body, exc.headers.get("Content-Type", ""))
            if isinstance(parsed, Mapping):
                message = str(parsed.get("error") or parsed.get("message") or f"HTTP {exc.code}")
            else:
                message = str(parsed) if parsed else f"HTTP {exc.code}"
            if exc.code == 401 and self.saved_token:
                try:
                    self.clear_session()
                except CLIError:
                    pass
            raise HTTPFailure(exc.code, message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CLIError(f"cannot reach API at {self.base_url}: {exc}", EXIT_NETWORK) from exc

    def _credentials(self) -> tuple[str, str]:
        username = self.username
        if not username:
            if not sys.stdin.isatty():
                raise CLIError("username is required (set CERTCTL_USERNAME)", EXIT_AUTH)
            username = input("Username: ").strip()
        password = self.password
        if password is None:
            if not sys.stdin.isatty():
                password = sys.stdin.readline().rstrip("\r\n")
            else:
                password = getpass.getpass("Password: ")
        if not username or not password:
            raise CLIError("username and password are required", EXIT_AUTH)
        self.username, self.password = username, password
        self.remember_secret(password)
        return username, password

    def login(self) -> dict[str, Any]:
        username, password = self._credentials()
        value, _ = self.request("POST", "/api/auth/login", payload={"username": username, "password": password}, authenticated=False)
        if isinstance(value, Mapping) and value.get("enabled") is False:
            raise CLIError("account is disabled", EXIT_AUTH)
        token = _token_from(value)
        if not token:
            raise CLIError("login response did not contain a bearer token", EXIT_AUTH)
        self.token = token
        self.identity = _identity_from(value)
        if not self.identity or not any(key in self.identity for key in ("role", "roles", "is_admin", "isAdmin")):
            return self.me()
        return self.identity

    def me(self) -> dict[str, Any]:
        if not self.token:
            self.login()
        value, _ = self.request("GET", "/api/auth/me")
        if isinstance(value, Mapping) and value.get("enabled") is False:
            raise CLIError("account is disabled", EXIT_AUTH)
        if isinstance(value, Mapping):
            self.identity = _identity_from(value)
        else:
            self.identity = {}
        return self.identity

    def ensure_identity(self) -> dict[str, Any]:
        return self.identity if self.identity is not None else self.me()

    def require_admin(self) -> dict[str, Any]:
        identity = self.ensure_identity()
        roles = identity.get("roles")
        role_values = [identity.get("role"), identity.get("user_role"), *(roles if isinstance(roles, list) else [])]
        role_values = {str(role).strip().lower() for role in role_values if role}
        if not (identity.get("is_admin") or identity.get("isAdmin") or "admin" in role_values):
            raise CLIError("administrator role required", EXIT_FORBIDDEN)
        return identity

    def require_agent_scope(self, agent: str | None) -> None:
        if not agent:
            return
        identity = self.ensure_identity()
        roles = identity.get("roles")
        role_values = [identity.get("role"), identity.get("user_role"), *(roles if isinstance(roles, list) else [])]
        role_values = {str(role).strip().lower() for role in role_values if role}
        is_admin = bool(identity.get("is_admin") or identity.get("isAdmin")) or bool(role_values & _ADMIN_ROLES)
        if not is_admin:
            raise CLIError("--agent is only available to administrators", EXIT_FORBIDDEN)

    def resolve_agent_id(self, value: str) -> int:
        """Resolve an administrator's numeric ID or agent name."""
        try:
            agent_id = int(value)
            if agent_id < 1:
                raise ValueError
            return agent_id
        except (TypeError, ValueError):
            pass
        listing, _ = self.request("GET", "/api/agents")
        items = listing.get("items", []) if isinstance(listing, Mapping) else []
        for item in items:
            if isinstance(item, Mapping) and str(item.get("name", "")) == value:
                try:
                    return int(item["id"])
                except (KeyError, TypeError, ValueError):
                    break
        raise CLIError(f"agent not found: {value}", EXIT_NOT_FOUND)


def _agent_payload(args: argparse.Namespace, client: APIClient) -> dict[str, Any]:
    agent = getattr(args, "agent", None)
    client.require_agent_scope(agent)
    return {"agent_id": client.resolve_agent_id(agent)} if agent else {}


def _password_for_user(*, from_stdin: bool = False) -> str:
    """Read a newly managed user's password without accepting it in argv."""
    password = os.environ.get("CERTCTL_NEW_PASSWORD") or os.environ.get("CERTCTL_USER_PASSWORD")
    if password is None and from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    if password is None:
        if not sys.stdin.isatty():
            raise CLIError("set CERTCTL_NEW_PASSWORD or use --password-stdin", EXIT_AUTH)
        password = getpass.getpass("New password: ")
        confirmation = getpass.getpass("Repeat new password: ")
        if password != confirmation:
            raise CLIError("passwords do not match", EXIT_AUTH)
    if not password:
        raise CLIError("new password cannot be empty", EXIT_AUTH)
    return password


def _admin_agent_id(client: APIClient, value: str) -> int:
    client.require_admin()
    return client.resolve_agent_id(value)


def _print(value: Any, *, as_json: bool = False) -> None:
    value = _public(value)
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(value)


def _command(args: argparse.Namespace, client: APIClient) -> Any:
    command = args.command
    if command == "login":
        result = client.login()
        client.save_session()
        return result
    if command == "logout":
        try:
            if client.token:
                try:
                    client.request("POST", "/api/auth/logout", payload={})
                except HTTPFailure as exc:
                    if exc.status != 401:
                        raise
        finally:
            client.clear_session()
        return {"logged_out": True}
    if command == "me":
        return client.me()
    if command in {"agents", "agent"}:
        client.require_admin()
        action = args.agent_command
        if action == "list":
            value, _ = client.request("GET", "/api/agents")
            return value
        if action == "create":
            value, _ = client.request("POST", "/api/agents", payload={"name": args.name})
            return value
        agent_id = _admin_agent_id(client, args.agent)
        if action == "show":
            value, _ = client.request("GET", f"/api/agents/{agent_id}")
            return value
        if action in {"update", "disable", "enable"}:
            payload: dict[str, Any] = {}
            if action == "update":
                if args.name is not None:
                    payload["name"] = args.name
                if args.status is not None:
                    payload["status"] = args.status
                if not payload:
                    raise CLIError("agents update needs --name or --status", EXIT_USAGE)
            else:
                payload["status"] = "disabled" if action == "disable" else "active"
            value, _ = client.request("PATCH", f"/api/agents/{agent_id}", payload=payload)
            return value
        raise CLIError(f"unknown agents action: {action}", EXIT_USAGE)
    if command in {"users", "user"}:
        client.require_admin()
        action = args.user_command
        if action == "list":
            if args.agent:
                agent_id = _admin_agent_id(client, args.agent)
                value, _ = client.request("GET", f"/api/agents/{agent_id}/users")
            else:
                value, _ = client.request("GET", "/api/users")
            return value
        if action == "create":
            if args.agent:
                if args.role != "agent":
                    raise CLIError("--agent creates an agent role; omit it for an administrator user", EXIT_USAGE)
                agent_id = _admin_agent_id(client, args.agent)
                path = f"/api/agents/{agent_id}/users"
                payload = {"username": args.username}
            else:
                path = "/api/users"
                payload = {"username": args.username, "role": args.role}
                if args.agent_id:
                    payload["agent_id"] = _admin_agent_id(client, args.agent_id)
            password = _password_for_user(from_stdin=args.password_stdin)
            client.remember_secret(password)
            payload["password"] = password
            value, _ = client.request("POST", path, payload=payload)
            return value
        user_id = args.user_id
        try:
            user_id = int(user_id)
            if user_id < 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise CLIError("user ID must be a positive integer", EXIT_USAGE) from exc
        if action == "show":
            value, _ = client.request("GET", f"/api/users/{user_id}")
            return value
        if action == "reset-password":
            value, _ = client.request("PATCH", f"/api/users/{user_id}", payload={"password": _password_for_user(from_stdin=args.password_stdin)})
            return value
        if action in {"disable", "enable"}:
            value, _ = client.request("PATCH", f"/api/users/{user_id}", payload={"status": "disabled" if action == "disable" else "active"})
            return value
        raise CLIError(f"unknown users action: {action}", EXIT_USAGE)
    # Authenticate every resource operation before constructing its request.
    # This also makes a missing credential fail with the same stable auth code
    # for issue, list, detail, download, and registration commands.
    client.ensure_identity()
    if command in {"issue", "cert-issue"}:
        payload: dict[str, Any] = {
            "type": args.type,
            "name": args.name,
            "key_type": args.key_type,
            **_agent_payload(args, client),
        }
        if args.days is not None:
            payload["days"] = args.days
        if args.san:
            payload["sans"] = args.san
        value, _ = client.request("POST", "/api/certificates", payload=payload)
        return value
    if command in {"list", "cert-list"}:
        query = {"type": args.type, **_agent_payload(args, client)}
        value, _ = client.request("GET", "/api/certificates", query=query)
        return value
    if command in {"show", "cert-show"}:
        value, _ = client.request("GET", f"/api/certificates/{args.id}")
        return value
    if command in {"download", "cert-download"}:
        if args.output == "-" and args.json:
            raise CLIError("--json cannot be combined with download --output -", EXIT_USAGE)
        query = {"format": args.format} if args.format else {}
        path = f"/api/certificates/{args.id}/download" if not args.file else f"/api/certificates/{args.id}/files/{urllib.parse.quote(args.file, safe='')}"
        body, headers = client.request("GET", path, query=query, raw=True)
        if not isinstance(body, bytes) or "json" in headers.get("content-type", "").lower():
            raise CLIError("download endpoint returned JSON instead of a file", EXIT_OUTPUT)
        disposition = headers.get("content-disposition", "")
        match = re.search(r'filename="?([^";]+)', disposition)
        filename = pathlib.Path(match.group(1)).name if match else (pathlib.Path(args.file).name if args.file else f"certificate-{args.id}.zip")
        if args.output == "-":
            binary_stdout = getattr(sys.stdout, "buffer", None)
            if binary_stdout is None:
                raise CLIError("binary stdout is unavailable; use --output FILE", EXIT_OUTPUT)
            binary_stdout.write(body)
            binary_stdout.flush()
            return None
        destination = pathlib.Path(args.output or filename).expanduser()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_TRUNC if args.force else os.O_EXCL
            fd = os.open(destination, flags, 0o600)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(body)
        except FileExistsError as exc:
            raise CLIError(f"output already exists: {destination} (use --force to replace)", EXIT_OUTPUT) from exc
        except OSError as exc:
            raise CLIError(f"cannot write {destination}: {exc}", EXIT_OUTPUT) from exc
        return {"path": str(destination), "filename": filename, "bytes": len(body)}
    if command in {"registration-list", "app-list"}:
        value, _ = client.request("GET", "/api/registrations", query=_agent_payload(args, client))
        return value
    if command in {"registration-create", "app-create"}:
        payload = {"name": args.name, "owner": args.owner or "", "notes": args.notes or "", **_agent_payload(args, client)}
        value, _ = client.request("POST", "/api/registrations", payload=payload)
        return value
    raise CLIError(f"unknown command: {command}", EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="certctl", description="CornTech CA + agent + certificate CLI")
    parser.add_argument("--url", help="API base URL (CERTCTL_URL)")
    parser.add_argument("--username", "-u", dest="auth_username", help="API username (CERTCTL_USERNAME)")
    parser.add_argument("--session-file", help="token cache path (CERTCTL_SESSION_FILE)")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="log in with environment or interactive credentials")
    sub.add_parser("logout", help="invalidate the bearer session and delete its local cache")
    sub.add_parser("me", help="show the authenticated identity")

    agents = sub.add_parser("agents", aliases=["agent"], help="administrator agent account management")
    agent_sub = agents.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("list", help="list agents")
    agent_create = agent_sub.add_parser("create", help="create an agent")
    agent_create.add_argument("name")
    agent_show = agent_sub.add_parser("show", help="show an agent")
    agent_show.add_argument("agent")
    agent_update = agent_sub.add_parser("update", help="update an agent")
    agent_update.add_argument("agent")
    agent_update.add_argument("--name")
    agent_update.add_argument("--status", choices=("active", "disabled"))
    for action, help_text in (("disable", "disable an agent"), ("enable", "enable an agent")):
        command_parser = agent_sub.add_parser(action, help=help_text)
        command_parser.add_argument("agent")

    users = sub.add_parser("users", aliases=["user"], help="administrator user account management")
    user_sub = users.add_subparsers(dest="user_command", required=True)
    user_list = user_sub.add_parser("list", help="list users")
    user_list.add_argument("--agent", help="filter by agent ID or name")
    user_create = user_sub.add_parser("create", help="create an API user")
    user_create.add_argument("username")
    user_create.add_argument("--role", choices=("admin", "agent"), default="agent")
    user_create.add_argument("--agent", help="create an agent user under this agent")
    user_create.add_argument("--agent-id", help="agent ID or name when creating via /api/users")
    user_create.add_argument("--password-stdin", action="store_true", help="read the new password from stdin")
    user_show = user_sub.add_parser("show", help="show a user")
    user_show.add_argument("user_id")
    user_reset = user_sub.add_parser("reset-password", help="replace a user's password")
    user_reset.add_argument("user_id")
    user_reset.add_argument("--password-stdin", action="store_true", help="read the new password from stdin")
    for action, help_text in (("disable", "disable a user"), ("enable", "enable a user")):
        command_parser = user_sub.add_parser(action, help=help_text)
        command_parser.add_argument("user_id")

    issue = sub.add_parser("issue", aliases=["cert-issue"], help="issue a client or server certificate")
    issue.add_argument("name")
    issue.add_argument("--type", choices=("server", "client"), default="server")
    issue.add_argument("--san", action="append", default=[], help="server SAN (repeatable)")
    issue.add_argument("--days", type=int)
    issue.add_argument("--key-type", choices=("rsa", "ec", "both"), default="both")
    issue.add_argument("--agent", help="agent ID or name (administrators only)")

    listing = sub.add_parser("list", aliases=["cert-list"], help="list certificates")
    listing.add_argument("--type", choices=("server", "client"))
    listing.add_argument("--agent", help="filter agent ID or name (administrators only)")

    show = sub.add_parser("show", aliases=["cert-show"], help="show a certificate")
    show.add_argument("id", type=int)

    download = sub.add_parser("download", aliases=["cert-download"], help="download a ZIP or one certificate file")
    download.add_argument("id", type=int)
    download_format = download.add_mutually_exclusive_group()
    download_format.add_argument("--file", help="download one filename from the certificate record")
    download_format.add_argument("--format", choices=("crt", "key", "p12", "pem", "bundle"))
    download.add_argument("--output", "-o", help="destination path, or - for stdout")
    download.add_argument("--force", action="store_true")

    reg_list = sub.add_parser("registration-list", aliases=["app-list"], help="list registered applications")
    reg_list.add_argument("--agent", help="filter agent ID or name (administrators only)")

    reg_create = sub.add_parser("registration-create", aliases=["app-create"], help="register an application")
    reg_create.add_argument("name")
    reg_create.add_argument("--owner")
    reg_create.add_argument("--notes")
    reg_create.add_argument("--agent", help="agent ID or name (administrators only)")
    return parser


def _normalize_global_options(argv: Sequence[str]) -> list[str]:
    """Permit global connection/output options after a subcommand as well."""
    values: list[str] = []
    remainder: list[str] = []
    value_options = {"--url", "--username", "-u", "--session-file", "--timeout"}
    index = 0
    argv = list(argv)
    while index < len(argv):
        item = argv[index]
        if item == "--json":
            values.append(item)
        elif item in value_options:
            if index + 1 >= len(argv):
                remainder.append(item)
            else:
                values.extend((item, argv[index + 1]))
                index += 1
        elif any(item.startswith(option + "=") for option in value_options):
            values.append(item)
        else:
            remainder.append(item)
        index += 1
    return values + remainder


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    if argv is None:
        argv = sys.argv[1:]
    argv = _normalize_global_options(argv)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    client: APIClient | None = None
    try:
        client = APIClient.from_environment(args)
        value = _command(args, client)
        if value is not None:
            _print(value, as_json=args.json)
        return EXIT_OK
    except CLIError as exc:
        message = str(exc)
        for secret in ((client.password, client.token, *client.secrets) if client else ()):
            if secret:
                message = message.replace(secret, "[redacted]")
        if args.json:
            print(json.dumps({"error": message, "code": exc.code}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"certctl: {message}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("certctl: interrupted", file=sys.stderr)
        return EXIT_API


if __name__ == "__main__":
    raise SystemExit(main())
