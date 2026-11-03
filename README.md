<div align="center">

<img src="assets/banner.svg" alt="mailcow-dav-mcp: contacts, calendar and tasks for AI assistants" width="100%">

<br>

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.10%2B-8A2BE2?style=for-the-badge&logo=anthropic&logoColor=white)](https://modelcontextprotocol.io/)
[![mailcow](https://img.shields.io/badge/mailcow-SOGo-F2A900?style=for-the-badge&logo=maildotru&logoColor=white)](https://mailcow.email/)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=for-the-badge)](LICENSE)

[![GitHub stars](https://img.shields.io/github/stars/OakNet-io/mailcow-dav-mcp?style=for-the-badge&logo=github&color=FACC15&labelColor=24292F)](https://github.com/OakNet-io/mailcow-dav-mcp/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/OakNet-io/mailcow-dav-mcp?style=for-the-badge&logo=github&color=F97316&labelColor=24292F)](https://github.com/OakNet-io/mailcow-dav-mcp/issues)
[![Last commit](https://img.shields.io/github/last-commit/OakNet-io/mailcow-dav-mcp?style=for-the-badge&logo=git&logoColor=white&color=0EA5E9&labelColor=24292F)](https://github.com/OakNet-io/mailcow-dav-mcp/commits/main)

**Works with**

![Claude Code](https://img.shields.io/badge/Claude_Code-D97757?style=flat-square&logo=claude&logoColor=white)
![Claude Desktop](https://img.shields.io/badge/Claude_Desktop-D97757?style=flat-square&logo=claude&logoColor=white)
![Codex](https://img.shields.io/badge/Codex-111111?style=flat-square&logo=openai&logoColor=white)
![ChatGPT](https://img.shields.io/badge/ChatGPT-10A37F?style=flat-square&logo=openai&logoColor=white)
![Any MCP client](https://img.shields.io/badge/any_MCP_client-6366F1?style=flat-square)

**🇬🇧 English** · [🇮🇹 Italiano](README.it.md)

</div>

---

An [MCP](https://modelcontextprotocol.io/) server that gives AI assistants access to the **📇 address book (CardDAV)**,
**📅 calendar (CalDAV)** and **✅ tasks** of a [mailcow](https://mailcow.email/) mailbox. It uses the SOGo DAV endpoint
that mailcow already publishes at `/SOGo/dav/<mailbox>/`, so you don't need to change anything on the mailcow side.

> [!TIP]
> If this project saves you time, give it a ⭐ on GitHub: it helps other mailcow users find it.

## 📚 Contents

- [✨ Features](#-features)
- [📋 Requirements](#-requirements)
- [🚀 Quick start](#-quick-start)
- [🔌 Connect your client](#-connect-your-client): Claude Code · Claude Desktop · Codex · ChatGPT · other HTTP clients
- [⚙️ Configuration](#️-configuration)
- [🔒 Security](#-security)
- [💡 Usage notes](#-usage-notes)
- [🩺 Troubleshooting](#-troubleshooting)
- [🛠️ Development](#️-development)
- [🆘 Support](#-support)

## ✨ Features

| 📇 Contacts | 📅 Calendar | ✅ Tasks |
|---|---|---|
| `list_addressbooks` · `list_contacts` (tag filter) | `list_calendars` | `list_tasks` |
| `search_contacts`: name, surname, email, phone, company, notes. Ignores case and accents. | `list_events`: date range, recurrences expanded | `get_task` |
| `get_contact` · `get_contact_photo` | `search_events` (text) | `create_task` |
| `create_contact` · `update_contact` · `delete_contact` | `get_event` | `update_task` (`completed=true`) |
| `list_categories` (tags in use) | `create_event`: attendees, reminders, RRULE, all-day | `delete_task` |
| `list_groups` · `add_to_group` · `remove_from_group` | `update_event` · `delete_event`: whole series or one occurrence | |
| `export_contacts` (vcf/csv) · `import_contacts` (vcf/csv, dry-run, de-duplication) | `respond_to_invitation`: accept, decline, tentative | |
| `check_folding` (SOGo folding bug) | `calendar="*"` queries all calendars | |

🩺 `dav_status` checks the credentials and lists your address books and calendars.

<details>
<summary><b>📇 Contact fields, groups and tags</b></summary>

<br>

**Fields**: prefix, given/middle/family name, suffix, nickname, typed emails and phones (`work:`, `cell:`…), company and
department, job title and role, notes, birthday, anniversary, spouse, assistant, IM (`telegram:@x`, `skype:id`…),
time zone, URLs, postal addresses (`type:street;city;region;zip;country`), tags and photo.

**Groups vs tags**: *groups* are Apple-style group vCards (what Apple Contacts and BusyContacts show).
*Tags* are the `CATEGORIES` of each contact (what SOGo shows). Both are supported.

</details>

## 📋 Requirements

- 🐄 A mailcow mailbox (any recent mailcow with SOGo enabled).
- ⚡ [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended), or Python 3.11+ with `pip`.
- 💻 macOS, Linux or Windows.

## 🚀 Quick start

### 1️⃣ Create a mailcow app password

Use an **app password**, not your mailbox password: you can limit it to DAV and revoke it at any time.

1. Log in to the mailcow UI as the mailbox user.
2. Open **Mailbox → Edit → App passwords** and add a new one.
3. Under **Allow for**, select only **DAV/SOGo**.
4. Copy the password. You need it in the next step.

### 2️⃣ Store the credentials

Keep them in a dedicated file that only your user can read, so the password never ends up in a client's config:

```bash
mkdir -p ~/.config/mailcow-dav-mcp
cat > ~/.config/mailcow-dav-mcp/.env <<'EOF'
MAILCOW_DAV_URL=https://mail.example.com
MAILCOW_DAV_USER=you@example.com
MAILCOW_DAV_PASSWORD=your-app-password
MAILCOW_DAV_TIMEZONE=Europe/Rome
EOF
chmod 600 ~/.config/mailcow-dav-mcp/.env
```

Every client then needs only `MAILCOW_DAV_ENV_FILE` pointing to this file. A template is in [.env.example](.env.example).

> [!IMPORTANT]
> In client configs, write the path in full (`/Users/you/...` or `/home/you/...`). Clients don't expand `~`.

### 3️⃣ Install and test

**Option A: no clone (recommended).** `uvx` downloads and runs the server on demand:

```bash
MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp@v0.2.1 mailcow-dav-mcp --check
```

**Option B: from a clone** (for development or to pin a version):

```bash
git clone https://github.com/OakNet-io/mailcow-dav-mcp.git
cd mailcow-dav-mcp
uv sync                                   # or: python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env && chmod 600 .env    # then edit .env
uv run mailcow-dav-mcp --check
```

✅ Expected output: `OK server=... user=...` followed by your address books and calendars.

> [!TIP]
> **Updating.** The commands pin a release tag (`@v0.2.1`): you always run a reviewed version, and a new
> commit on `main` never reaches you by surprise. `uvx` caches it in `~/.cache/uv`, so later starts are instant.
> To update, change the tag in the command and in your client config to the latest
> [release](https://github.com/OakNet-io/mailcow-dav-mcp/tags).
> To always follow `main` instead, drop the tag and add `--refresh` to fetch new commits:
> `uvx --refresh --from git+https://github.com/OakNet-io/mailcow-dav-mcp mailcow-dav-mcp --check`.
> `uv cache clean mailcow-dav-mcp` removes the cached copy.

> [!NOTE]
> With a clone, replace `uvx --from git+…` in the client configs below with
> `uv --directory /path/to/mailcow-dav-mcp run mailcow-dav-mcp`. A clone also reads `.env` from the project folder.

## 🔌 Connect your client

<details open>
<summary><img src="https://img.shields.io/badge/Claude_Code-D97757?style=flat-square&logo=claude&logoColor=white" alt=""> <b>Claude Code</b></summary>

<br>

```bash
claude mcp add mailcow-dav --scope user \
  -e MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  -- uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp@v0.2.1 mailcow-dav-mcp
```

Check with `claude mcp list`, or type `/mcp` inside Claude Code.
`--scope user` makes the server available in every project. Leave it out to add it to the current project only.

</details>

<details open>
<summary><img src="https://img.shields.io/badge/Claude_Desktop-D97757?style=flat-square&logo=claude&logoColor=white" alt=""> <b>Claude Desktop</b></summary>

<br>

Open **Settings → Developer → Edit Config**, or edit the file directly:

- 🍎 macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- 🪟 Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "mailcow-dav": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/OakNet-io/mailcow-dav-mcp@v0.2.1", "mailcow-dav-mcp"],
      "env": {
        "MAILCOW_DAV_ENV_FILE": "/Users/you/.config/mailcow-dav-mcp/.env"
      }
    }
  }
}
```

Quit Claude Desktop completely and start it again. The tools appear under the tools icon in the chat box.
A copy of this snippet is in [claude_desktop_config.example.json](claude_desktop_config.example.json).

> [!WARNING]
> Claude Desktop starts servers with a minimal `PATH`. If it reports `spawn uvx ENOENT`, put the full path of `uvx`
> in `command` (find it with `which uvx`, e.g. `/opt/homebrew/bin/uvx` or `~/.local/bin/uvx`).

</details>

<details open>
<summary><img src="https://img.shields.io/badge/Codex-111111?style=flat-square&logo=openai&logoColor=white" alt=""> <b>Codex (CLI & IDE)</b></summary>

<br>

```bash
codex mcp add mailcow-dav \
  --env MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  -- uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp@v0.2.1 mailcow-dav-mcp
```

Or add it by hand to `~/.codex/config.toml`. The Codex CLI and the IDE extension share this file:

```toml
[mcp_servers.mailcow-dav]
command = "uvx"
args = ["--from", "git+https://github.com/OakNet-io/mailcow-dav-mcp@v0.2.1", "mailcow-dav-mcp"]
env = { MAILCOW_DAV_ENV_FILE = "/Users/you/.config/mailcow-dav-mcp/.env" }
```

Check with `codex mcp list`, or `/mcp` inside a Codex session.

</details>

<details open>
<summary><img src="https://img.shields.io/badge/ChatGPT-10A37F?style=flat-square&logo=openai&logoColor=white" alt=""> <b>ChatGPT</b></summary>

<br>

ChatGPT connects only to **remote** MCP servers over HTTPS: it can't start a local command. Custom MCP servers are
available on ChatGPT web for Business and Enterprise/Edu workspaces, subject to the workspace permissions
(**Create plugins with MCPs**). See OpenAI's guide:
[Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461).

**1. Start the server in HTTP mode** on your machine or on a server you control. It listens on `127.0.0.1` only:

```bash
MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp@v0.2.1 mailcow-dav-mcp --transport http --port 8000
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`.

**2. Make it reachable by ChatGPT without exposing it.** Choose one:

- 🛡️ **OpenAI Secure MCP Tunnel (recommended).** The server stays private. Follow the tunnel setup in the guide above
  and point the tunnel at `http://127.0.0.1:8000/mcp`.
- 🔐 **A reverse proxy that adds OAuth**, for example an identity-aware proxy in front of the server.

If the tunnel or proxy forwards a public host name, allow it with `MAILCOW_DAV_MCP_ALLOWED_HOSTS=mcp.example.com`.
Otherwise the server rejects the requests with `421` (DNS-rebinding protection).

**3. Add it in ChatGPT.**

1. Open **Plugins**, select **+**, then **Add custom MCP server**.
2. Enter a name. Under **Connection**, enter the HTTPS URL, or select **Tunnel** and choose your tunnel.
3. Configure authentication, then check **I understand and want to continue**.
4. Select **Create as a plugin** and review the discovered tools.
5. Install the plugin, start a new chat, type `@` and select it.

In the legacy flow (**Settings → Apps → Create**), enter the endpoint, select **Scan Tools**, then **Create**.

> [!CAUTION]
> Never publish the endpoint without authentication. Anyone with the URL could read and change your contacts and
> calendar. ChatGPT supports OAuth or no authentication, so use the tunnel or an OAuth proxy.

</details>

<details>
<summary>🌐 <b>Other HTTP clients</b></summary>

<br>

Any client that supports Streamable HTTP can use the HTTP mode. To listen on an address other than loopback you must
set a bearer token, otherwise the server refuses to start. Generate the token once and keep it in a private file:

```bash
mkdir -p ~/.config/mailcow-dav-mcp
(umask 077; openssl rand -hex 32 > ~/.config/mailcow-dav-mcp/token)
export MAILCOW_DAV_MCP_TOKEN="$(cat ~/.config/mailcow-dav-mcp/token)"
MAILCOW_DAV_MCP_ALLOWED_HOSTS=mcp.example.com \
  mailcow-dav-mcp --transport http --host 127.0.0.1 --port 8000
```

Publish it through a reverse proxy with TLS on the same machine, forwarding `https://mcp.example.com/mcp` to
`http://127.0.0.1:8000/mcp`. Use `--host 0.0.0.0` only on a private network, and never without TLS in front: the token
travels in a header. `MAILCOW_DAV_MCP_ALLOWED_HOSTS` is the public host name the clients use. Without it the server
answers `421`.
On the client machine, read the same token:

- Claude Code: `claude mcp add --transport http mailcow-dav https://mcp.example.com/mcp -H "Authorization: Bearer $(cat ~/.config/mailcow-dav-mcp/token)"`
- Codex: `export MAILCOW_DAV_MCP_TOKEN="$(cat ~/.config/mailcow-dav-mcp/token)"` in the shell that starts Codex, then
  `codex mcp add mailcow-dav --url https://mcp.example.com/mcp --bearer-token-env-var MAILCOW_DAV_MCP_TOKEN`

</details>

## ⚙️ Configuration

| Variable | Default | Description |
|---|---|---|
| 🌍 `MAILCOW_DAV_URL` | **required** | Base URL of mailcow, e.g. `https://mail.example.com` |
| 👤 `MAILCOW_DAV_USER` | **required** | Full mailbox address |
| 🔑 `MAILCOW_DAV_PASSWORD` | **required** | App password with DAV/SOGo access |
| 📄 `MAILCOW_DAV_ENV_FILE` | — | `.env` file to load. Variables already in the environment win. |
| 🕒 `MAILCOW_DAV_TIMEZONE` | `Europe/Rome` | IANA time zone for dates without an offset |
| 🧭 `MAILCOW_DAV_PRINCIPAL_PATH` | `/SOGo/dav/<user>/` | Override if the DAV root is elsewhere |
| 🔏 `MAILCOW_DAV_VERIFY_SSL` | `1` | `0` disables TLS verification (testing only) |
| ⏱️ `MAILCOW_DAV_TIMEOUT` | `30` | HTTP timeout to the DAV server, in seconds |
| 📦 `MAILCOW_DAV_EXPORT_DIR` | `~/mailcow-dav-exports` | Default export folder, and the folder for relative paths. Import, export and local photos may only use files in this folder or in `MAILCOW_DAV_LOCAL_DIRS`. |
| 📂 `MAILCOW_DAV_LOCAL_DIRS` | — | Extra folders the tools may read or write, separated by `:` (`;` on Windows), e.g. `~/Downloads` |
| 👁️ `MAILCOW_DAV_READ_ONLY` | `0` | `1` hides every tool that changes contacts, calendars or tasks. Search, read and export stay. |
| 📏 `MAILCOW_DAV_MAX_RESPONSE_MB` | `64` | Largest DAV response accepted, after decompression |
| 📥 `MAILCOW_DAV_MAX_IMPORT_MB` / `_CARDS` | `20` / `10000` | Largest import file and number of cards per import |
| 🚚 `MAILCOW_DAV_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (same as `--transport`) |
| 🔌 `MAILCOW_DAV_MCP_HOST` / `_PORT` / `_PATH` | `127.0.0.1` / `8000` / `/mcp` | HTTP listen address and endpoint path |
| 🎟️ `MAILCOW_DAV_MCP_TOKEN` | — | Bearer token for every HTTP request. Mandatory when the host is not loopback. |
| 🏷️ `MAILCOW_DAV_MCP_ALLOWED_HOSTS` | — | Extra `Host` names accepted in HTTP mode (tunnel or proxy), comma-separated |

Command line: `mailcow-dav-mcp [--check] [--transport stdio|http] [--host H] [--port P] [--path /mcp]`

## 🔒 Security

| | |
|---|---|
| 🔑 **Credentials** | Use an app password limited to DAV/SOGo. Keep it in a `chmod 600` file referenced by `MAILCOW_DAV_ENV_FILE`, not in a client config that you might sync or share. |
| 🔄 **Concurrent edits** | Every update and delete is conditional on the ETag (`If-Match`). Writes without a known ETag are refused. Pass the `etag` from `get_*`/`search_*` to update/delete tools: if another client changed the item after you read it, the call fails instead of overwriting that change. |
| 🖼️ **Photos from URLs** | Downloads go only to public IP addresses. Each redirect is checked, the connection is pinned to the verified IP (no DNS rebinding), and the download stops at 2 MB. Photo references stored on the server are never read from the local disk. |
| 📁 **Paths** | Address book and calendar IDs must be a single path segment. Resource names never come from a UID found in an imported file. Exports never overwrite a file unless `overwrite=true`, and are written atomically with mode `600`. |
| 🌐 **HTTP mode** | Loopback by default, token required on any other address, `Host`/`Origin` checks. Without a token, every process on the machine can use the endpoint: set one even on loopback. |
| 🗂️ **Local files** | In every mode, import, export and local photos are limited to `MAILCOW_DAV_EXPORT_DIR` and `MAILCOW_DAV_LOCAL_DIRS`. |
| 🧷 **DAV server answers** | Only paths on the configured host and inside the requested collection are accepted, so the credentials never go elsewhere. Responses and imports have size limits. Recurrence rules that never end are not expanded. |
| 🗑️ **Deletes and prompt injection** | Notes, titles and invitations come from other people and can contain instructions. Tools carry MCP annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`), so clients can ask before changes. Keep approval prompts on for write tools, or set `MAILCOW_DAV_READ_ONLY=1`. Photos stored as external URLs are downloaded only with `fetch_url=true`. |

> [!IMPORTANT]
> Found a vulnerability? Open a [private security advisory](https://github.com/OakNet-io/mailcow-dav-mcp/security/advisories/new)
> instead of a public issue.

## 💡 Usage notes

- 🗓️ Dates are ISO 8601. Without an offset, `MAILCOW_DAV_TIMEZONE` is used. Explicit offsets (`+02:00`) are converted
  to that time zone, keeping the same instant. For all-day events, `end` is exclusive.
- 📝 In updates, lists (`emails`, `phones`, `addresses`, `attendees`, `urls`, `categories`) **replace** the existing ones.
- ⏳ Changing only text fields of an event leaves its dates untouched. Moving `start` keeps the duration. Turning an
  all-day event into a timed one requires `start` with a time.
- 🔁 Recurrences are expanded on the client. To change or delete one occurrence, pass the `recurrence_id` from
  `list_events` as `occurrence`. It keeps working after the occurrence has been moved. On a non-recurring event
  `occurrence` is an error: the tool never falls back to the whole event. `rrule=""` turns a series into a single
  event and drops its exceptions. Each series is expanded up to 2000 occurrences per request.
- ⏰ New task reminders are relative to the due date, or to the start date when there is no due date. Existing
  reminders keep their anchor, unless that date is removed.
- ✉️ `respond_to_invitation` sets your PARTSTAT. SOGo sends the reply to the organizer. SOGo also sends the invitations
  when you create an event with attendees.
- 🔍 `search_contacts` scans the whole address book on the client, because SOGo's server-side query matches only some
  fields. The first search takes about 2–3 seconds for ~700 contacts. Later searches download only the cards whose
  ETag changed.
- 📤 `export_contacts` as `vcf` is an exact copy of every resource, groups included. `csv` contains contacts only.
  Resources the server lists but doesn't return are reported in `missing`. In CSV, list items are separated by ` | `
  (`\|` is a literal bar), and cells that start with `=`, `+`, `-` or `@` get a leading `'` so spreadsheets don't run
  them as formulas. `import_contacts` reverses both.
- 📥 `import_contacts` imports contacts first, then groups, with members rewritten to the UIDs actually used.
  Members that match no contact are listed in `unresolved_members`, in the dry run too.

<details>
<summary>🐛 <b>SOGo folding bug</b></summary>

<br>

SOGo re-serializes every vCard. If a line is exactly 75 characters long (or 149, 223…), SOGo folds it with an empty
continuation line. Apple Contacts and BusyContacts then merge it with the next field (e.g. country `IT` +
`CATEGORIES:…` → `ITCATEGORIES:…`). A client can't fix this: `create_contact`/`update_contact` return a `warning`, and
`check_folding` lists the affected contacts. Fix: change the field length (e.g. `Italia` instead of `IT`).

</details>

## 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| 🚫 `401/403 — wrong credentials…` | Check user and app password. The app password must allow **DAV/SOGo**. |
| 🔌 `DAV server unreachable (ConnectError…)` | Check `MAILCOW_DAV_URL` (host name, `https://`), DNS and network or VPN. `ConnectTimeout`: firewall or wrong port. |
| ❓ `Missing variables: MAILCOW_DAV_URL…` | The client doesn't pass the variables: check `MAILCOW_DAV_ENV_FILE` (full path, no `~`). |
| 🧩 `spawn uvx ENOENT` (Claude Desktop) | Use the full path of `uvx` in `command`. |
| 🔄 `412 … ETag` / "modified by another client" | The item changed after you read it. Read it again and retry. |
| 🌐 HTTP `421 Misdirected Request` | Add the public host name to `MAILCOW_DAV_MCP_ALLOWED_HOSTS`. |
| 🎟️ HTTP `401 unauthorized` | Send `Authorization: Bearer <MAILCOW_DAV_MCP_TOKEN>`. |

Server messages are in English.

## 🛠️ Development

```bash
uv sync --extra dev
uv run --extra dev pytest
```

For manual integration tests, run [Radicale](https://radicale.org/) locally. The server expects the SOGo layout
`<principal>/Contacts/<id>/` and `<principal>/Calendar/<id>/`, so give the test user write access to nested
collections and create them once:

```bash
printf 'tester:secret\n' > users
cat > rights <<'EOF2'
[root]
user: .+
collection:
permissions: R

[owner]
user: .+
collection: {user}(/.*)?
permissions: RrWw
EOF2
cat > config <<EOF2
[server]
hosts = 127.0.0.1:5232
[auth]
type = htpasswd
htpasswd_filename = $PWD/users
htpasswd_encryption = plain
[rights]
type = from_file
file = $PWD/rights
[storage]
filesystem_folder = $PWD/data
EOF2
uvx radicale --config ./config &
sleep 3
B=http://127.0.0.1:5232; U=tester:secret
curl -u "$U" -X PROPFIND -H "Depth: 0" "$B/tester/" -o /dev/null   # creates the principal
curl -u "$U" -X MKCOL "$B/tester/Contacts/"
curl -u "$U" -X MKCOL "$B/tester/Calendar/"
curl -u "$U" -X MKCOL -H "Content-Type: application/xml" "$B/tester/Contacts/personal/" --data \
  '<D:mkcol xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav"><D:set><D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop></D:set></D:mkcol>'
curl -u "$U" -X MKCALENDAR "$B/tester/Calendar/personal/"
```

Then set `MAILCOW_DAV_URL=http://127.0.0.1:5232`, `MAILCOW_DAV_USER=tester`, `MAILCOW_DAV_PRINCIPAL_PATH=/tester/`.

| Module | Role |
|---|---|
| `dav.py` | Minimal WebDAV client (PROPFIND, REPORT, GET, conditional PUT/DELETE) |
| `carddav.py` | Contacts, groups, photos, client-side search |
| `caldav.py` | Events, occurrences, invitations, tasks |
| `bulk.py` | vCard/CSV import and export |
| `tzgen.py` | VTIMEZONE generation |
| `safety.py` | Path, URL, file and ETag checks |
| `server.py` | MCP tools, stdio and HTTP transports |

Contributions are welcome: open an issue to discuss bigger changes first, and add a test for every fix.

## 🆘 Support

- 📖 **Documentation**: this README · [Italiano](README.it.md)
- 🐞 **Issues**: [GitHub Issues](https://github.com/OakNet-io/mailcow-dav-mcp/issues)
- 💬 **Discussions**: [GitHub Discussions](https://github.com/OakNet-io/mailcow-dav-mcp/discussions)
- 🔐 **Security**: [private advisory](https://github.com/OakNet-io/mailcow-dav-mcp/security/advisories/new)

## 🙏 Acknowledgements

- [mailcow](https://mailcow.email/) and [SOGo](https://www.sogo.nu/) for the mail and groupware stack
- [Model Context Protocol](https://modelcontextprotocol.io/) and its [Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [vobject](https://github.com/py-vobject/vobject), [icalendar](https://github.com/collective/icalendar),
  [recurring-ical-events](https://github.com/niccokunzmann/python-recurring-ical-events), [httpx](https://www.python-httpx.org/), [lxml](https://lxml.de/)

## 📄 License

Released under the [MIT License](LICENSE).

<div align="center">

**⭐ Star the repo if it helps you · 🐄 Made for the mailcow community**

</div>
