<div align="center">

<img src="assets/banner.svg" alt="mailcow-dav-mcp: rubrica, calendario e task per gli assistenti AI" width="100%">

<br>

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.10%2B-8A2BE2?style=for-the-badge&logo=anthropic&logoColor=white)](https://modelcontextprotocol.io/)
[![mailcow](https://img.shields.io/badge/mailcow-SOGo-F2A900?style=for-the-badge&logo=maildotru&logoColor=white)](https://mailcow.email/)
[![Licenza](https://img.shields.io/badge/Licenza-MIT-22C55E?style=for-the-badge)](LICENSE)

[![GitHub stars](https://img.shields.io/github/stars/OakNet-io/mailcow-dav-mcp?style=for-the-badge&logo=github&color=FACC15&labelColor=24292F)](https://github.com/OakNet-io/mailcow-dav-mcp/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/OakNet-io/mailcow-dav-mcp?style=for-the-badge&logo=github&color=F97316&labelColor=24292F)](https://github.com/OakNet-io/mailcow-dav-mcp/issues)
[![Ultimo commit](https://img.shields.io/github/last-commit/OakNet-io/mailcow-dav-mcp?style=for-the-badge&logo=git&logoColor=white&color=0EA5E9&labelColor=24292F)](https://github.com/OakNet-io/mailcow-dav-mcp/commits/main)

**Funziona con**

![Claude Code](https://img.shields.io/badge/Claude_Code-D97757?style=flat-square&logo=claude&logoColor=white)
![Claude Desktop](https://img.shields.io/badge/Claude_Desktop-D97757?style=flat-square&logo=claude&logoColor=white)
![Codex](https://img.shields.io/badge/Codex-111111?style=flat-square&logo=openai&logoColor=white)
![ChatGPT](https://img.shields.io/badge/ChatGPT-10A37F?style=flat-square&logo=openai&logoColor=white)
![Qualsiasi client MCP](https://img.shields.io/badge/qualsiasi_client_MCP-6366F1?style=flat-square)

[🇬🇧 English](README.md) · **🇮🇹 Italiano**

</div>

---

Server [MCP](https://modelcontextprotocol.io/) che dà agli assistenti AI accesso alla **📇 rubrica (CardDAV)**, al
**📅 calendario (CalDAV)** e ai **✅ task** di una mailbox [mailcow](https://mailcow.email/). Usa l'endpoint DAV di SOGo
che mailcow pubblica già su `/SOGo/dav/<mailbox>/`: lato mailcow non serve cambiare nulla.

> [!TIP]
> Se il progetto ti fa risparmiare tempo, lascia una ⭐ su GitHub: aiuta altri utenti mailcow a trovarlo.

## 📚 Indice

- [✨ Funzionalità](#-funzionalità)
- [📋 Requisiti](#-requisiti)
- [🚀 Avvio rapido](#-avvio-rapido)
- [🔌 Collegare il client](#-collegare-il-client): Claude Code · Claude Desktop · Codex · ChatGPT · altri client HTTP
- [⚙️ Configurazione](#️-configurazione)
- [🔒 Sicurezza](#-sicurezza)
- [💡 Note d'uso](#-note-duso)
- [🩺 Risoluzione problemi](#-risoluzione-problemi)
- [🛠️ Sviluppo](#️-sviluppo)
- [🆘 Supporto](#-supporto)

## ✨ Funzionalità

| 📇 Rubrica | 📅 Calendario | ✅ Task |
|---|---|---|
| `list_addressbooks` · `list_contacts` (filtro per tag) | `list_calendars` | `list_tasks` |
| `search_contacts`: nome, cognome, email, telefono, azienda, note. Ignora maiuscole e accenti. | `list_events`: intervallo di date, ricorrenze espanse | `get_task` |
| `get_contact` · `get_contact_photo` | `search_events` (testo) | `create_task` |
| `create_contact` · `update_contact` · `delete_contact` | `get_event` | `update_task` (`completed=true`) |
| `list_categories` (tag in uso) | `create_event`: invitati, promemoria, RRULE, giornata intera | `delete_task` |
| `list_groups` · `add_to_group` · `remove_from_group` | `update_event` · `delete_event`: intera serie o singola occorrenza | |
| `export_contacts` (vcf/csv) · `import_contacts` (vcf/csv, dry-run, deduplica) | `respond_to_invitation`: accetta, rifiuta, forse | |
| `check_folding` (bug di folding di SOGo) | `calendar="*"` interroga tutti i calendari | |

🩺 `dav_status` verifica le credenziali ed elenca rubriche e calendari.

<details>
<summary><b>📇 Campi contatto, gruppi e tag</b></summary>

<br>

**Campi**: prefisso, nome/secondo nome/cognome, suffisso, nickname, email e telefoni tipizzati (`work:`, `cell:`…),
azienda e reparto, qualifica e ruolo, note, compleanno, anniversario, coniuge, assistente, IM (`telegram:@x`,
`skype:id`…), fuso orario, URL, indirizzi postali (`tipo:via;città;provincia;cap;paese`), tag e foto.

**Gruppi e tag**: i *gruppi* sono vCard di gruppo in stile Apple (quelli che vedono Contatti di Apple e BusyContacts).
I *tag* sono le `CATEGORIES` del singolo contatto (quelli che vede SOGo). Sono supportati entrambi.

</details>

## 📋 Requisiti

- 🐄 Una mailbox mailcow (qualsiasi versione recente con SOGo attivo).
- ⚡ [uv](https://docs.astral.sh/uv/getting-started/installation/) (consigliato), oppure Python 3.11+ con `pip`.
- 💻 macOS, Linux o Windows.

## 🚀 Avvio rapido

### 1️⃣ Crea una app password su mailcow

Usa una **app password**, non la password della mailbox: puoi limitarla al DAV e revocarla quando vuoi.

1. Accedi all'interfaccia di mailcow come utente della mailbox.
2. Apri **Mailbox → Modifica → App passwords** e aggiungine una.
3. In **Allow for** seleziona solo **DAV/SOGo**.
4. Copia la password: ti serve al passo successivo.

### 2️⃣ Salva le credenziali

Mettile in un file dedicato leggibile solo dal tuo utente, così la password non finisce nella configurazione dei client:

```bash
mkdir -p ~/.config/mailcow-dav-mcp
cat > ~/.config/mailcow-dav-mcp/.env <<'EOF'
MAILCOW_DAV_URL=https://mail.example.com
MAILCOW_DAV_USER=tu@example.com
MAILCOW_DAV_PASSWORD=la-tua-app-password
MAILCOW_DAV_TIMEZONE=Europe/Rome
EOF
chmod 600 ~/.config/mailcow-dav-mcp/.env
```

A ogni client basta poi `MAILCOW_DAV_ENV_FILE` con il percorso di questo file. Un modello è in [.env.example](.env.example).

> [!IMPORTANT]
> Nelle configurazioni dei client scrivi il percorso completo (`/Users/tu/...` o `/home/tu/...`): i client non espandono `~`.

### 3️⃣ Installa e prova

**Opzione A: senza clonare (consigliata).** `uvx` scarica ed esegue il server al bisogno:

```bash
MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp mailcow-dav-mcp --check
```

**Opzione B: da un clone** (per sviluppo o per fissare una versione):

```bash
git clone https://github.com/OakNet-io/mailcow-dav-mcp.git
cd mailcow-dav-mcp
uv sync                                   # oppure: python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env && chmod 600 .env    # poi modifica .env
uv run mailcow-dav-mcp --check
```

✅ Output atteso: `OK server=... user=...` seguito da rubriche e calendari.

> [!NOTE]
> Con un clone, nelle configurazioni qui sotto sostituisci `uvx --from git+…` con
> `uv --directory /percorso/mailcow-dav-mcp run mailcow-dav-mcp`. Un clone legge anche il `.env` nella cartella del progetto.

## 🔌 Collegare il client

<details open>
<summary><img src="https://img.shields.io/badge/Claude_Code-D97757?style=flat-square&logo=claude&logoColor=white" alt=""> <b>Claude Code</b></summary>

<br>

```bash
claude mcp add mailcow-dav --scope user \
  -e MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  -- uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp mailcow-dav-mcp
```

Verifica con `claude mcp list`, oppure scrivi `/mcp` in Claude Code.
`--scope user` rende il server disponibile in tutti i progetti. Senza, lo aggiunge solo al progetto corrente.

</details>

<details open>
<summary><img src="https://img.shields.io/badge/Claude_Desktop-D97757?style=flat-square&logo=claude&logoColor=white" alt=""> <b>Claude Desktop</b></summary>

<br>

Apri **Impostazioni → Sviluppatore → Modifica configurazione**, oppure modifica direttamente il file:

- 🍎 macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- 🪟 Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "mailcow-dav": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/OakNet-io/mailcow-dav-mcp", "mailcow-dav-mcp"],
      "env": {
        "MAILCOW_DAV_ENV_FILE": "/Users/tu/.config/mailcow-dav-mcp/.env"
      }
    }
  }
}
```

Chiudi completamente Claude Desktop e riaprilo. I tool compaiono sotto l'icona degli strumenti nella chat.
Una copia di questo esempio è in [claude_desktop_config.example.json](claude_desktop_config.example.json).

> [!WARNING]
> Claude Desktop avvia i server con un `PATH` minimo. Se segnala `spawn uvx ENOENT`, metti in `command` il percorso
> completo di `uvx` (lo trovi con `which uvx`, es. `/opt/homebrew/bin/uvx` o `~/.local/bin/uvx`).

</details>

<details open>
<summary><img src="https://img.shields.io/badge/Codex-111111?style=flat-square&logo=openai&logoColor=white" alt=""> <b>Codex (CLI e IDE)</b></summary>

<br>

```bash
codex mcp add mailcow-dav \
  --env MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  -- uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp mailcow-dav-mcp
```

Oppure aggiungilo a mano in `~/.codex/config.toml`. La CLI di Codex e l'estensione IDE usano lo stesso file:

```toml
[mcp_servers.mailcow-dav]
command = "uvx"
args = ["--from", "git+https://github.com/OakNet-io/mailcow-dav-mcp", "mailcow-dav-mcp"]
env = { MAILCOW_DAV_ENV_FILE = "/Users/tu/.config/mailcow-dav-mcp/.env" }
```

Verifica con `codex mcp list`, oppure `/mcp` in una sessione Codex.

</details>

<details open>
<summary><img src="https://img.shields.io/badge/ChatGPT-10A37F?style=flat-square&logo=openai&logoColor=white" alt=""> <b>ChatGPT</b></summary>

<br>

ChatGPT si collega solo a server MCP **remoti** in HTTPS: non può avviare un comando locale. I server MCP
personalizzati sono disponibili su ChatGPT web per i workspace Business ed Enterprise/Edu, secondo i permessi del
workspace (**Create plugins with MCPs**). Vedi la guida di OpenAI:
[Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461).

**1. Avvia il server in modalità HTTP** sul tuo computer o su un server che controlli. Ascolta solo su `127.0.0.1`:

```bash
MAILCOW_DAV_ENV_FILE=$HOME/.config/mailcow-dav-mcp/.env \
  uvx --from git+https://github.com/OakNet-io/mailcow-dav-mcp mailcow-dav-mcp --transport http --port 8000
```

L'endpoint MCP è `http://127.0.0.1:8000/mcp`.

**2. Rendilo raggiungibile da ChatGPT senza esporlo.** Scegli una strada:

- 🛡️ **Secure MCP Tunnel di OpenAI (consigliato).** Il server resta privato. Segui la configurazione del tunnel nella
  guida qui sopra e puntalo a `http://127.0.0.1:8000/mcp`.
- 🔐 **Un reverse proxy che aggiunge OAuth**, ad esempio un proxy con verifica dell'identità davanti al server.

Se il tunnel o il proxy inoltrano un nome host pubblico, autorizzalo con `MAILCOW_DAV_MCP_ALLOWED_HOSTS=mcp.example.com`.
Altrimenti il server rifiuta le richieste con `421` (protezione DNS rebinding).

**3. Aggiungilo in ChatGPT.**

1. Apri **Plugins**, seleziona **+**, poi **Add custom MCP server**.
2. Inserisci un nome. In **Connection** inserisci l'URL HTTPS, oppure seleziona **Tunnel** e scegli il tuo tunnel.
3. Configura l'autenticazione, poi spunta **I understand and want to continue**.
4. Seleziona **Create as a plugin** e controlla i tool trovati.
5. Installa il plugin, apri una nuova chat, scrivi `@` e selezionalo.

Nel flusso legacy (**Settings → Apps → Create**) inserisci l'endpoint, seleziona **Scan Tools**, poi **Create**.

> [!CAUTION]
> Non pubblicare mai l'endpoint senza autenticazione: chiunque conosca l'URL potrebbe leggere e modificare rubrica e
> calendario. ChatGPT supporta OAuth oppure nessuna autenticazione, quindi usa il tunnel o un proxy OAuth.

</details>

<details>
<summary>🌐 <b>Altri client HTTP</b></summary>

<br>

Qualsiasi client che supporta Streamable HTTP può usare la modalità HTTP. Per ascoltare su un indirizzo diverso da
loopback devi impostare un token bearer, altrimenti il server non parte. Genera il token una volta e tienilo in un file
privato:

```bash
mkdir -p ~/.config/mailcow-dav-mcp
(umask 077; openssl rand -hex 32 > ~/.config/mailcow-dav-mcp/token)
export MAILCOW_DAV_MCP_TOKEN="$(cat ~/.config/mailcow-dav-mcp/token)"
MAILCOW_DAV_MCP_ALLOWED_HOSTS=mcp.example.com \
  mailcow-dav-mcp --transport http --host 0.0.0.0 --port 8000
```

`MAILCOW_DAV_MCP_ALLOWED_HOSTS` è il nome host pubblico usato dai client: senza, il server risponde `421`.
Sulla macchina del client leggi lo stesso token:

- Claude Code: `claude mcp add --transport http mailcow-dav https://mcp.example.com/mcp -H "Authorization: Bearer $(cat ~/.config/mailcow-dav-mcp/token)"`
- Codex: `export MAILCOW_DAV_MCP_TOKEN="$(cat ~/.config/mailcow-dav-mcp/token)"` nella shell che avvia Codex, poi
  `codex mcp add mailcow-dav --url https://mcp.example.com/mcp --bearer-token-env-var MAILCOW_DAV_MCP_TOKEN`

Metti davanti un reverse proxy con TLS: il token viaggia in un header.

</details>

## ⚙️ Configurazione

| Variabile | Default | Descrizione |
|---|---|---|
| 🌍 `MAILCOW_DAV_URL` | **obbligatoria** | URL base di mailcow, es. `https://mail.example.com` |
| 👤 `MAILCOW_DAV_USER` | **obbligatoria** | Indirizzo completo della mailbox |
| 🔑 `MAILCOW_DAV_PASSWORD` | **obbligatoria** | App password con accesso DAV/SOGo |
| 📄 `MAILCOW_DAV_ENV_FILE` | — | File `.env` da caricare. Le variabili già presenti nell'ambiente hanno la precedenza. |
| 🕒 `MAILCOW_DAV_TIMEZONE` | `Europe/Rome` | Fuso IANA per le date senza offset |
| 🧭 `MAILCOW_DAV_PRINCIPAL_PATH` | `/SOGo/dav/<user>/` | Da cambiare se la radice DAV è altrove |
| 🔏 `MAILCOW_DAV_VERIFY_SSL` | `1` | `0` disattiva la verifica TLS (solo per test) |
| ⏱️ `MAILCOW_DAV_TIMEOUT` | `30` | Timeout HTTP verso il server DAV, in secondi |
| 📦 `MAILCOW_DAV_EXPORT_DIR` | `~/mailcow-dav-exports` | Cartella predefinita degli export. In modalità HTTP è l'unica cartella che i tool possono leggere o scrivere. |
| 🚚 `MAILCOW_DAV_MCP_TRANSPORT` | `stdio` | `stdio` o `http` (come `--transport`) |
| 🔌 `MAILCOW_DAV_MCP_HOST` / `_PORT` / `_PATH` | `127.0.0.1` / `8000` / `/mcp` | Indirizzo di ascolto HTTP e percorso dell'endpoint |
| 🎟️ `MAILCOW_DAV_MCP_TOKEN` | — | Token bearer richiesto su ogni richiesta HTTP. Obbligatorio se l'host non è loopback. |
| 🏷️ `MAILCOW_DAV_MCP_ALLOWED_HOSTS` | — | Nomi `Host` aggiuntivi accettati in modalità HTTP (tunnel o proxy), separati da virgola |

Riga di comando: `mailcow-dav-mcp [--check] [--transport stdio|http] [--host H] [--port P] [--path /mcp]`

## 🔒 Sicurezza

| | |
|---|---|
| 🔑 **Credenziali** | Usa una app password limitata a DAV/SOGo. Tienila in un file `chmod 600` indicato da `MAILCOW_DAV_ENV_FILE`, non nella configurazione di un client che potresti sincronizzare o condividere. |
| 🔄 **Modifiche concorrenti** | Ogni modifica e cancellazione è condizionata all'ETag (`If-Match`). Le scritture senza ETag noto vengono rifiutate. Passa ai tool di modifica/cancellazione l'`etag` restituito da `get_*`/`search_*`: se un altro client ha cambiato l'elemento dopo la tua lettura, l'operazione fallisce invece di sovrascrivere. |
| 🖼️ **Foto da URL** | I download vanno solo verso indirizzi IP pubblici. Ogni redirect viene verificato, la connessione usa l'IP già controllato (niente DNS rebinding) e il download si ferma a 2 MB. I riferimenti a foto salvati sul server non vengono mai letti dal disco locale. |
| 📁 **Percorsi** | Gli ID di rubriche e calendari devono essere un solo segmento di percorso. I nomi delle risorse non derivano mai dall'UID di un file importato. Gli export non sovrascrivono un file esistente senza `overwrite=true` e vengono scritti in modo atomico con permessi `600`. |
| 🌐 **Modalità HTTP** | Loopback per default, token obbligatorio su qualsiasi altro indirizzo, controllo degli header `Host`/`Origin`, accesso ai file limitato a `MAILCOW_DAV_EXPORT_DIR`. |
| 🗑️ **Cancellazioni** | I tool chiedono all'assistente di confermare prima con te. Lascia attive per questi tool le richieste di approvazione del client. |

> [!IMPORTANT]
> Hai trovato una vulnerabilità? Apri una [segnalazione di sicurezza privata](https://github.com/OakNet-io/mailcow-dav-mcp/security/advisories/new)
> invece di una issue pubblica.

## 💡 Note d'uso

- 🗓️ Date in ISO 8601. Senza offset si usa `MAILCOW_DAV_TIMEZONE`. Gli offset espliciti (`+02:00`) vengono convertiti
  in quel fuso mantenendo lo stesso istante. Per gli eventi di un giorno intero, `end` è esclusivo.
- 📝 Negli aggiornamenti le liste (`emails`, `phones`, `addresses`, `attendees`, `urls`, `categories`) **sostituiscono**
  quelle esistenti.
- ⏳ Cambiando solo i campi di testo di un evento le date restano invariate. Spostando `start` la durata si conserva.
  Per trasformare un evento di un giorno intero in uno con orario serve `start` con l'ora.
- 🔁 Le ricorrenze vengono espanse lato client. Per modificare o cancellare una sola occorrenza passa come `occurrence`
  il `recurrence_id` restituito da `list_events`: funziona anche dopo che l'occorrenza è stata spostata. Su un evento
  non ricorrente `occurrence` dà errore: il tool non agisce mai sull'intero evento al suo posto. `rrule=""` trasforma
  una serie in un evento singolo e ne toglie le eccezioni. Ogni serie è espansa fino a 2000 occorrenze per richiesta.
- ⏰ I nuovi promemoria dei task sono relativi alla scadenza, oppure all'inizio se la scadenza manca. Quelli esistenti
  mantengono il loro riferimento, a meno che quella data venga tolta.
- ✉️ `respond_to_invitation` imposta il tuo PARTSTAT e SOGo invia la risposta all'organizzatore. È sempre SOGo a
  spedire gli inviti quando crei un evento con invitati.
- 🔍 `search_contacts` scorre tutta la rubrica lato client, perché la ricerca lato server di SOGo considera solo alcuni
  campi. Con ~700 contatti la prima ricerca richiede circa 2–3 secondi; le successive scaricano solo le schede con
  ETag cambiato.
- 📤 `export_contacts` in `vcf` è una copia esatta di ogni scheda, gruppi inclusi. Il `csv` contiene solo i contatti.
  Le risorse elencate dal server ma non restituite sono riportate in `missing`. Nel CSV gli elementi delle liste sono
  separati da ` | ` (`\|` è una barra letterale) e le celle che iniziano con `=`, `+`, `-` o `@` hanno un `'` davanti,
  così i fogli di calcolo non le eseguono come formule. `import_contacts` annulla entrambe le cose.
- 📥 `import_contacts` importa prima i contatti, poi i gruppi, con i membri riscritti sugli UID effettivi. I membri che
  non corrispondono a nessun contatto sono elencati in `unresolved_members`, anche in dry-run.

<details>
<summary>🐛 <b>Bug di folding di SOGo</b></summary>

<br>

SOGo riserializza ogni vCard. Se una riga è lunga esattamente 75 caratteri (o 149, 223…), SOGo la ripiega con una
riga di continuazione vuota. Contatti di Apple e BusyContacts la uniscono poi al campo successivo (es. paese `IT` +
`CATEGORIES:…` → `ITCATEGORIES:…`). Un client non può correggerlo: `create_contact`/`update_contact` restituiscono un
`warning` e `check_folding` elenca i contatti interessati. Rimedio: cambia la lunghezza del campo (es. `Italia` invece di `IT`).

</details>

## 🩺 Risoluzione problemi

| Sintomo | Soluzione |
|---|---|
| 🚫 `401/403 — credenziali errate…` | Controlla utente e app password. L'app password deve consentire **DAV/SOGo**. |
| ❓ `Variabili mancanti: MAILCOW_DAV_URL…` | Il client non passa le variabili: controlla `MAILCOW_DAV_ENV_FILE` (percorso completo, niente `~`). |
| 🧩 `spawn uvx ENOENT` (Claude Desktop) | Usa il percorso completo di `uvx` in `command`. |
| 🔄 `412 … ETag` / "modificato da un altro client" | L'elemento è cambiato dopo la tua lettura. Rileggilo e riprova. |
| 🌐 HTTP `421 Misdirected Request` | Aggiungi il nome host pubblico a `MAILCOW_DAV_MCP_ALLOWED_HOSTS`. |
| 🎟️ HTTP `401 unauthorized` | Invia `Authorization: Bearer <MAILCOW_DAV_MCP_TOKEN>`. |

## 🛠️ Sviluppo

```bash
uv sync --extra dev
uv run --extra dev pytest
```

Per test d'integrazione manuali avvia [Radicale](https://radicale.org/) in locale. Il server si aspetta la struttura
di SOGo `<principal>/Contacts/<id>/` e `<principal>/Calendar/<id>/`: dai all'utente di test i permessi sulle
collezioni annidate e creale una volta:

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
curl -u "$U" -X PROPFIND -H "Depth: 0" "$B/tester/" -o /dev/null   # crea il principal
curl -u "$U" -X MKCOL "$B/tester/Contacts/"
curl -u "$U" -X MKCOL "$B/tester/Calendar/"
curl -u "$U" -X MKCOL -H "Content-Type: application/xml" "$B/tester/Contacts/personal/" --data \
  '<D:mkcol xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav"><D:set><D:prop><D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop></D:set></D:mkcol>'
curl -u "$U" -X MKCALENDAR "$B/tester/Calendar/personal/"
```

Poi imposta `MAILCOW_DAV_URL=http://127.0.0.1:5232`, `MAILCOW_DAV_USER=tester`, `MAILCOW_DAV_PRINCIPAL_PATH=/tester/`.

| Modulo | Ruolo |
|---|---|
| `dav.py` | Client WebDAV minimale (PROPFIND, REPORT, GET, PUT/DELETE condizionati) |
| `carddav.py` | Contatti, gruppi, foto, ricerca lato client |
| `caldav.py` | Eventi, occorrenze, inviti, task |
| `bulk.py` | Import ed export vCard/CSV |
| `tzgen.py` | Generazione delle VTIMEZONE |
| `safety.py` | Controlli su percorsi, URL, file ed ETag |
| `server.py` | Tool MCP, trasporti stdio e HTTP |

I contributi sono benvenuti: per modifiche importanti apri prima una issue, e aggiungi un test per ogni correzione.

## 🆘 Supporto

- 📖 **Documentazione**: questo README · [English](README.md)
- 🐞 **Issue**: [GitHub Issues](https://github.com/OakNet-io/mailcow-dav-mcp/issues)
- 💬 **Discussioni**: [GitHub Discussions](https://github.com/OakNet-io/mailcow-dav-mcp/discussions)
- 🔐 **Sicurezza**: [segnalazione privata](https://github.com/OakNet-io/mailcow-dav-mcp/security/advisories/new)

## 🙏 Ringraziamenti

- [mailcow](https://mailcow.email/) e [SOGo](https://www.sogo.nu/) per lo stack di posta e groupware
- [Model Context Protocol](https://modelcontextprotocol.io/) e il suo [SDK Python](https://github.com/modelcontextprotocol/python-sdk)
- [vobject](https://github.com/py-vobject/vobject), [icalendar](https://github.com/collective/icalendar),
  [recurring-ical-events](https://github.com/niccokunzmann/python-recurring-ical-events), [httpx](https://www.python-httpx.org/), [lxml](https://lxml.de/)

## 📄 Licenza

Rilasciato con [licenza MIT](LICENSE).

<div align="center">

**⭐ Metti una stella se ti è utile · 🐄 Fatto per la community mailcow**

</div>
