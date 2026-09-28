# 📡 Telegram → Stremio

<p align="center">
  <img src="stremio_addon/icon.png" alt="Stremio Telegram addon icon" width="180" />
</p>

**Your Telegram videos, available in Stremio.**

A self-hosted addon that connects to your Telegram user account, discovers joined private broadcast channels, and indexes uploaded videos for browsing, searching, and streaming through your server.

**🔎 Searchable catalog · 🔄 Background indexing · 🧰 Debug dashboard · 🐳 Docker · 🏠 Home Assistant**

---

## 🧭 Quick navigation

- [Technologies](#-technologies)
- [Folder layout](#-folder-layout)
- [Configuration](#-configuration)
- [Prepare your credentials](#-prepare-your-credentials)
- [Install with Docker Compose](#-install-with-docker-compose)
- [Install with Home Assistant](#-install-with-home-assistant)
- [Debug dashboard](#-debug-dashboard)

## 🛠️ Technologies

| Technology | Role |
| --- | --- |
| **Python 3.12** | Application runtime |
| **FastAPI + Uvicorn** | Stremio API, streaming endpoints, and debug server |
| **Telethon** | Telegram user authentication and channel access |
| **SQLite FTS5** | Persistent index and full-text search |
| **HTTPX** | Asynchronous HTTP requests |
| **Docker Compose** | Standalone container deployment |
| **Home Assistant Supervisor** | Add-on installation, configuration, and lifecycle |

Videos stream through your server without transcoding. Playback compatibility depends on the codecs supported by your Stremio device.

## 📂 Folder layout

> **Application code lives in `stremio_addon/`. Home Assistant packaging lives in `addon/`.**

| Path | Purpose |
| --- | --- |
| `stremio_addon/` | Core Stremio application: API, Telegram integration, indexing, streaming, and debug dashboard |
| `addon/` | Home Assistant package: configuration, startup wrapper, Dockerfile, documentation, and changelog |
| `addon/config.yaml` | Home Assistant options, supported architecture, image, and version |
| `tests/` | Automated application and release tests |
| `scripts/` | Release version and changelog tooling |
| `generate_session.py` | Interactive Telegram session generator |
| `repository.json` | Home Assistant custom repository metadata |

The container build copies `stremio_addon/` into its internal `addon` Python package. Both installation methods run the same application.

## ⚙️ Configuration

Set these values in `.env` for Docker Compose. In Home Assistant, enter the corresponding options in the add-on's **Configuration** tab.

**Mandatory** is a boolean: `true` means you must supply a value; `false` means a default is available.

| Environment variable | Description | Mandatory | Default | Example |
| --- | --- | :---: | --- | --- |
| `ADDON_URL` | Base URL reachable by Stremio; use HTTPS for deployment. May include a path prefix, but no credentials, query, or fragment. | `true` | — | `https://telegram.example.com` |
| `API_KEY` | Access key with at least 32 characters: letters, digits, underscores, or hyphens. Generate a unique value. | `true` | — | Generate with the command below |
| `API_ID` | Positive Telegram application ID. | `true` | — | `123456` |
| `API_HASH` | Telegram application hash. | `true` | — | `YOUR_TELEGRAM_API_HASH` |
| `USER_SESSION_STRING` | Complete authorized Telethon StringSession from the session generator. | `true` | — | `YOUR_TELETHON_STRING_SESSION` |
| `DEBUG_ENABLED` | Enable the separate debug dashboard. | `false` | `true` | `false` |
| `DEBUG_HOST` | Dashboard bind address. Keep the default for Docker port forwarding. | `false` | `0.0.0.0` | `0.0.0.0` |
| `CACHE_MB` | Disk chunk-cache limit in MiB; `0` disables new cache writes. | `false` | `512` | `1024` |
| `CHANNEL_IDS` | Comma-separated negative IDs limiting which joined private broadcast channels are indexed. Blank selects all eligible channels. | `false` | Empty | `-1001234567890,-1009876543210` |
| `AI_SEARCH_ENABLED` | Adds the separate **Telegram AI Search** catalog and builds a semantic index in the background. | `false` | `false` | `true` |
| `GEMINI_API_KEY` | Gemini API credential; required only when AI search is enabled. | Conditional | Empty | `YOUR_GEMINI_API_KEY` |
| `AI_SEARCH_PREFIX_ENABLED` | Require a leading `AI` word (case insensitive) for searches in the AI catalog. | `false` | `false` | `true` |
| `DATA_DIR` | Persistent index and cache directory. Home Assistant manages this automatically; it is not a UI option. | `false` | `/data` standalone; `/data/stremio` in Home Assistant | `/data` |

Use the variable names shown above. Set all environment variables in uppercase. Existing lowercase Home Assistant options remain readable during upgrades.

### 🎯 Selecting channels

Leave `CHANNEL_IDS` empty to discover all joined private broadcast channels, including archived dialogs. To limit indexing, copy the full negative channel IDs from the protected status endpoint into `CHANNEL_IDS`, then restart.

Selecting a channel does not join it. Public channels and groups are excluded. Removing a channel from the selection removes its indexed entries when discovery runs; selecting it again starts a fresh history scan.

### ✨ AI search

Set `AI_SEARCH_ENABLED=true` and provide `GEMINI_API_KEY` to add a separate **Telegram AI Search** catalog to Stremio. The existing **Telegram Videos** catalog keeps its ordinary text search. With `AI_SEARCH_PREFIX_ENABLED=true`, enter a search such as `AI someone relives the same day` in the AI catalog; searches without a separate leading `AI` word return no results there and do not call Gemini. The prefix is case insensitive.

AI search generates and stores embeddings for indexed titles, filenames, and caption excerpts. For entries without a meaningful caption, it may ask Gemini for a short description when the title can be identified confidently. This background indexing uses Gemini even if prefix mode is enabled. Search queries also use Gemini to retrieve and rank indexed entries. Only indexed entries in currently selected channels can be returned. Titles and caption excerpts are sent to Gemini; Telegram credentials, channel IDs, playback URLs, and video bytes are not. The debug dashboard displays indexing progress. New and edited entries are indexed incrementally, and search works with already processed entries while the initial index fills.

If Gemini returns HTTP 429, the add-on pauses all Gemini requests with a saved, increasing cooldown that survives restarts. During that pause, the AI catalog falls back to local text matches. The debug dashboard shows the retry countdown. If 429s continue after the cooldown, check your project's model quotas in Google AI Studio.

## 🔑 Prepare your credentials

Complete this once before either installation method.

### 1. Get Telegram application credentials

Sign in at [my.telegram.org](https://my.telegram.org), open **API development tools**, and obtain your **API ID** and **API hash**.

### 2. Download the project

On a trusted computer with Git and Python 3.12:

```sh
git clone https://github.com/HaCStream/stremio-addon.git
cd stremio-addon
python -m venv .venv
```

Activate the virtual environment:

| Platform | Command |
| --- | --- |
| Linux / macOS | `source .venv/bin/activate` |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |

Install dependencies and generate the session:

```sh
pip install -r requirements.txt
python generate_session.py
```

Enter your API credentials, phone number, Telegram login code, and two-step verification password if requested. Save the generated value as `USER_SESSION_STRING`.

### 3. Generate an addon access key

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save the output as `API_KEY`.

> 🔐 Keep the session string, API key, and installation URL private. Use a dedicated session for this addon and run only one instance with that session.

## 🐳 Install with Docker Compose

**You need:** Docker Engine with Docker Compose, the project checkout, and the credentials prepared above.

### 1. Configure the environment

From the repository root, copy the example:

```sh
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env`.

Edit `.env` and replace the placeholders:

```dotenv
ADDON_URL=https://telegram.example.com
API_KEY=REPLACE_WITH_YOUR_GENERATED_ACCESS_KEY
API_ID=123456
API_HASH=YOUR_TELEGRAM_API_HASH
USER_SESSION_STRING=YOUR_TELETHON_STRING_SESSION

DEBUG_ENABLED=true
DEBUG_HOST=0.0.0.0
CACHE_MB=512
# CHANNEL_IDS=-1001234567890,-1009876543210
```

### 2. Build and start

The included `compose.yaml` is ready to use:

```yaml
services:
  addon:
    build: .
    env_file: .env
    ports:
      - "127.0.0.1:8000:8000"
      - "127.0.0.1:8001:8001"
    volumes:
      - telegram-data:/data
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  telegram-data:
```

```sh
docker compose up --build -d
docker compose logs -f addon
```

The named volume preserves the index and cache across container restarts and rebuilds.
The app always listens on container ports 8000 and 8001. To use different host
ports, change the left-hand port in `compose.yaml` (for example,
`127.0.0.1:9000:8000`).

### 3. Configure HTTPS

The supplied Compose configuration publishes both ports on the Docker host's loopback interface. Point a reverse proxy running on that host at `127.0.0.1:8000`.

For example, with Caddy installed on the host:

```caddyfile
telegram.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Point the domain's DNS to your server and make ports 80 and 443 reachable by Caddy. Set `ADDON_URL` to the matching HTTPS base URL.

Preserve Range headers, allow long streaming requests, and disable proxy caching and response buffering. Avoid access logs containing the full credential-bearing URL. If using a URL prefix, strip that prefix before forwarding to the application.

If the proxy runs in another container, connect both services to a shared Docker network and forward to `addon:8000`.

### 4. Connect Stremio

Paste this address into Stremio's addon installation field, replacing the domain and key:

```text
https://telegram.example.com/YOUR_API_KEY/manifest.json
```

Initial indexing runs in the background, so results fill in as the scan progresses.

### 🔄 Update a Compose installation

From the repository directory:

```sh
git pull --ff-only
docker compose up --build -d
```

## 🏠 Install with Home Assistant

**You need:** a Home Assistant installation with the add-on store and an **amd64 / x86-64** host. The current Home Assistant package does not declare ARM support.

### 1. Add the custom repository

Open **Settings → Add-ons → Add-on Store**, then open the **⋮** menu and choose **Repositories**.

Paste this URL and select **Add**:

```text
https://github.com/HaCStream/stremio-addon
```

You can also use this shortcut:

[Add repository to Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FHaCStream%2Fstremio-addon)

### 2. Install the add-on

Find **Stremio Telegram** in the add-on store and select **Install**. Refresh the store if it has not appeared yet.

### 3. Configure it

Open the add-on's **Configuration** tab. Enter the credentials from [Prepare your credentials](#-prepare-your-credentials), along with your external URL.

Example YAML configuration:

```yaml
ADDON_URL: "https://telegram.example.com"
API_KEY: "REPLACE_WITH_YOUR_GENERATED_ACCESS_KEY"
API_ID: 123456
API_HASH: "YOUR_TELEGRAM_API_HASH"
USER_SESSION_STRING: "YOUR_TELETHON_STRING_SESSION"
debug:
  DEBUG_ENABLED: true
  DEBUG_HOST: "0.0.0.0"
CACHE_MB: 512
CHANNEL_IDS: ""
```

Save the configuration. Enable **Start on boot**, start the add-on, and check its **Log** tab.

Home Assistant stores application data in `/data/stremio`. Restart the add-on after changing configuration.

### 4. Configure the connection

The app listens on internal ports 8000 and 8001. The add-on's **Network** section
maps them to host ports; change those mappings if needed. Point your HTTPS reverse proxy at:

```text
http://HOME_ASSISTANT_IP:8000
```

Use the proxy settings described in the Docker section. The Stremio endpoint must be reachable by your client; Home Assistant ingress is not used.

Install the addon in Stremio with:

```text
https://telegram.example.com/YOUR_API_KEY/manifest.json
```

### 5. Open the dashboard

With `DEBUG_ENABLED: true`, select **Open Web UI** on the add-on page and sign in with your `API_KEY`.

The button uses the dashboard's mapped host port. You can also open `http://HOME_ASSISTANT_IP:8001` directly, using your chosen host port if you changed the mapping.

## 🧰 Debug dashboard

The dashboard runs on its own listener and works independently of `ADDON_URL`.

| Installation | Default dashboard address |
| --- | --- |
| Docker Compose, opened on the Docker host | `http://127.0.0.1:8001` |
| Home Assistant | `http://HOME_ASSISTANT_IP:8001` |

For a remote Docker host, use an SSH tunnel to reach the loopback-bound dashboard:

```sh
ssh -L 8001:127.0.0.1:8001 user@YOUR_DOCKER_HOST
```

Then open `http://127.0.0.1:8001` locally.

- **🔌 Connection:** inspect Telegram connection and indexing state.
- **📋 Channels:** see which channels are included in queries.
- **🔎 Search:** inspect matching results and diagnostic details without playback.
- **👁️ Credentials:** reveal or hide the API key while entering it.
- **🔄 Sync now:** trigger channel discovery and catch-up indexing after joining a channel or adding a video.
- **📝 Activity:** review recent addon activity.

For connection and indexing details, use the protected endpoint:

```text
https://telegram.example.com/YOUR_API_KEY/status
```

The `/healthz` endpoint checks that the HTTP server is running; it does not confirm Telegram readiness.


Find it useful? You support is welcome!
https://buymeacoffee.com/hacstudio
