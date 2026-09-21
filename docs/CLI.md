# `certctl` CLI

`scripts/certctl.py` is the dependency-free command-line client for the
single-machine API. It uses the same account and ownership rules as Web
Management. The client is intentionally stateless with respect to passwords;
after `login` it stores only a short-lived bearer token in a user-owned mode
`0600` session file.

```bash
export CERTCTL_URL=http://127.0.0.1:8080
export CERTCTL_USERNAME=admin
read -r -s CERTCTL_PASSWORD; export CERTCTL_PASSWORD; echo
python3 scripts/certctl.py login
python3 scripts/certctl.py me
```

Set `CERTCTL_TOKEN` for non-interactive jobs. `CERTCTL_SESSION_FILE` overrides
the token cache location. Without an environment password, `login` asks with
`getpass`; a password is never accepted as a command-line argument. New user
passwords use `CERTCTL_NEW_PASSWORD` (or `CERTCTL_USER_PASSWORD`), or
`--password-stdin`.

Certificate and application operations are available to both roles:

```bash
python3 scripts/certctl.py issue api.example.dev --san www.example.dev
python3 scripts/certctl.py list --type server
python3 scripts/certctl.py show 12
python3 scripts/certctl.py download 12 --output api.zip
python3 scripts/certctl.py registration-create orders-api --owner platform
```

Only an administrator can choose another agent. The argument accepts an agent
ID or name and is translated to the API's `agent_id` field:

```bash
python3 scripts/certctl.py issue api.example.dev --agent partner-a
python3 scripts/certctl.py list --agent 3
```

Administrator account management is grouped under `agents` and `users`:

```bash
python3 scripts/certctl.py agents list
python3 scripts/certctl.py agents create partner-a
python3 scripts/certctl.py agents disable partner-a
python3 scripts/certctl.py agents enable 3
python3 scripts/certctl.py users list --agent partner-a
read -r -s CERTCTL_NEW_PASSWORD; export CERTCTL_NEW_PASSWORD; echo
python3 scripts/certctl.py users create partner-user --agent partner-a
printf '%s\n' 'another password' | python3 scripts/certctl.py users reset-password 7 --password-stdin
python3 scripts/certctl.py users disable 7
```

`--json` emits stable machine-readable output. A successful command exits 0;
usage errors exit 2, authentication failures 3, missing records 4, network
errors 5, API errors 6, authorization failures 7, and local output errors 8.
Secrets are redacted from JSON and text output. `logout` invalidates the
server session and removes the local token cache.

The administrator compatibility wrappers `gen_root_cert.sh`,
`gen_server_cert.sh`, and `gen_client_cert.sh` continue to publish the
traditional `out/<name>/` links and timestamped snapshots. The wrapper name
`root` is reserved because it conflicts with the CA root filenames; use the
API/`certctl` path when a leaf certificate must have the Common Name `root`.
