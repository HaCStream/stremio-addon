# 📡 Telegram → Stremio

<p align="center">
  <img src="stremio_addon/icon.png" alt="Stremio Telegram addon icon" width="180" />
</p>

**Your Telegram videos, available in Stremio / Nuvio.**

A self-hosted addon that connects to your Telegram user account, discovers account chats (channels, groups, private conversations, bots and Saved Messages), and indexes uploaded videos for browsing, searching, and streaming through your server.

Since Stremio and Nuvio use the same underlying addon architecture and manifest format, this addon can work for both apps!

**🔎 Searchable catalog · 🔄 Background indexing · 🧰 Developer UI · 🐳 Docker · 🏠 Home Assistant**

---

## 🧭 Quick navigation

- [Technologies](#-technologies)
- [Folder layout](#-folder-layout)
- [Configuration](#-configuration)
- [Prepare your credentials](#-prepare-your-credentials)
- [Install with Docker Compose](#-install-with-docker-compose)
- [Install with Home Assistant](#-install-with-home-assistant)
- [Developer UI](#-developer-ui)

## 🛠️ Technologies

| Technology | Role |
| --- | --- |
| **Python 3.12** | Application runtime |
| **FastAPI + Uvicorn** | Stremio API, streaming endpoints, and Developer UI server |
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
| `stremio_addon/` | Core Stremio application: API, Telegram integration, indexing, streaming, and Developer UI |
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
| `ADDON_ACCESS_KEY` | Access key with at least 32 characters: letters, digits, underscores, or hyphens. Generate a unique value. | `true` | — | Generate with the command below |
| `TELEGRAM_APP_API_ID` | Positive Telegram application ID. | `true` | — | `123456` |
| `TELEGRAM_APP_API_HASH` | Telegram application hash. | `true` | — | `YOUR_TELEGRAM_APP_API_HASH` |
| `USER_SESSION_STRING` | Complete authorized Telethon StringSession from the session generator. | `true` | — | `YOUR_TELETHON_STRING_SESSION` |
| `DEV_UI_ENABLED` | Enable the separate Developer UI. | `false` | `true` | `false` |
| `DEV_UI_HOST` | Dashboard bind address. Keep the default for Docker port forwarding. | `false` | `0.0.0.0` | `0.0.0.0` |
| `DEV_UI_PASSWORD` | Optional custom Developer UI login password. Blank uses `ADDON_ACCESS_KEY`. Does not change the Stremio/Nuvio access key. Ignored when `SKIP_DEV_UI_AUTH=true`. | `false` | Empty | `YOUR_CUSTOM_PASSWORD` |
| `SKIP_DEV_UI_AUTH` | Open the Developer UI without a password; anyone with dashboard access can search and request a sync. | `false` | `false` | `true` |
| `CACHE_MB` | Disk chunk-cache limit in MiB; `0` disables new cache writes. | `false` | `512` | `1024` |
| `CHANNEL_IDS` | Comma-separated Telegram chat IDs restricting indexing and live search. Blank selects all accessible account chats. | `false` | Empty | `-1001234567890,-1009876543210` |
| `DISPLAY_COLLECTION` | Show Telegram Videos and Telegram Series collections on the main screen in Stremio/Nuvio. Search remains available when disabled. Restart and refresh/reinstall the addon in your client after changing it. | `false` | `false` | `true` |
| `AI_SEARCH_ENABLED` | Adds separate **Telegram AI Movies** and **Telegram AI Series** catalogs using online search. | `false` | `false` | `true` |
| `GEMINI_API_KEY` | Gemini API credential; required only when AI search is enabled. | Conditional | Empty | `YOUR_GEMINI_API_KEY` |
| `REQUIRE_AI_SUFFIX_FOR_AI_SEARCH` | Require a separate final `AI` word (case insensitive) before calling Gemini, preventing unfinished searches from using API quota while typing. Applies to AI catalogs and Developer UI AI search; the suffix is removed before sending. Set `false` to allow searches without it. | `false` | `true` | `true` |
| `TMDB_API_KEY` | Optional TMDB v3 API key. Enables saved show matching and Telegram sources for public catalog episodes. | `false` | Empty | `YOUR_TMDB_V3_API_KEY` |
| `DATA_DIR` | Persistent index and cache directory. Home Assistant manages this automatically; it is not a UI option. | `false` | `/data` standalone; `/data/stremio` in Home Assistant | `/data` |

Use the variable names shown above. Set all environment variables in uppercase. Existing lowercase Home Assistant options remain readable during upgrades.

### 🔗 TMDB show matching

Set `TMDB_API_KEY` to a TMDB **v3 API key** (not the API Read Access Token). In Home Assistant, this option is under **TMDB show matching**. Restart and refresh/reinstall the addon in Nuvio or Stremio so it advertises TMDB stream IDs.

The background matcher checks existing and newly indexed shows every minute. It searches using the extracted message title and filename aliases, comparing Hebrew, English, original and alternative TMDB names. One exact title match is saved automatically; multiple exact matches can be distinguished by an available year. Other results require review in the Developer UI's **TMDB matching** tab. Choose a candidate or enter a TMDB TV show ID, then save. Manual choices apply to every episode grouped under that Telegram show and are never replaced automatically.

Saved mappings connect a show to its TMDB and, where available, IMDb ID. When another catalog requests `tt1234567:1:2`, `tmdb:42:1:2`, or `tmdb:tv:42:1:2`, the addon returns files for the corresponding season and episode from the currently enabled channels. Playback uses the saved mapping without contacting TMDB. Missing episodes return no source. Telegram's own catalog and IDs remain usable. This does not change which metadata provider the client uses or guarantee that it combines duplicate search cards.

Mappings, review candidates and retry timestamps survive restarts. Unmatched/ambiguous shows are retried after one day; matched shows need no repeated lookup. API errors back off, and rate-limit responses pause matching. Clearing the key disables new lookups while preserving saved sources.

The review tab supports season and episode offsets: **Telegram number = catalog number + offset**. An offset of `1` maps catalog season 1 to Telegram season 2. Offsets apply to the entire show; arbitrary episode reorderings and different season boundaries are not handled automatically. You can disable matching for an incorrectly identified show. Existing explicit per-file IMDb mappings take precedence over a show mapping.

Only extracted title aliases (or your manually entered search title) are sent to TMDB; full captions, channel identities and media are not sent. This product uses the TMDB API but is not endorsed or certified by TMDB.

### 🎯 Selecting chats

Leave `CHANNEL_IDS` empty to scan accessible account history: joined public/private channels and groups, private conversations, bot chats, Saved Messages and archived dialogs. History is examined for video attachments; non-video text and tag replies are not indexed.

To restrict both indexing and live searches, copy the chat IDs from the Developer UI's **Chats** tab into `CHANNEL_IDS`, then restart. Private chats, bots and Saved Messages use positive user IDs; regular groups use negative IDs; supergroups/channels use `-100…` IDs. For example: `CHANNEL_IDS=123456789,-1001234567890`.

Selecting a chat does not join it or grant access. Left groups, deleted users and migrated group placeholders are excluded. Removing a source removes its indexed entries when discovery runs; selecting it again starts a fresh history scan.

### 🔎 Hybrid search

Text catalog searches, Developer UI text searches and movie/episode source lookups combine the local index with an on-demand Telegram search. Once every selected chat has completed its initial scan and the query has local matches, Telegram is searched only for messages newer than the oldest successfully checked chat snapshot, with a **two-minute overlap**. Incomplete indexes, chats without a saved scan time, and queries with no local matches retain full-history searches. Scan times persist across restarts; live-search hits never advance them. Successful identical Telegram queries (including empty results) are cached in memory for **60 seconds** and simultaneous identical requests share one search. New uploads may be absent until that cache expires.

Live hits are saved locally and deduplicated by chat/message ID, without advancing history checkpoints. Catalog results retain series grouping. External-catalog lookups try localized, English/original and shortened names, plus padded/unpadded and Hebrew episode variants. Telegram queries use the same request pacing and flood-wait cooldown as indexing and yield to streaming. The live-search phase has a **12-second budget**; on a timeout or Telegram failure, available indexed/partial results remain usable. Unfiltered searches inspect at most three 100-message pages per query, collecting at most 100 videos; filtered searches query selected chats directly. These bounded searches do not guarantee every available version will be returned.

Movie matching ranks exact title/year matches first, accepts strong titles with missing years and conservative subtitle variants, and rejects known conflicting years to avoid mixing remakes. Explicit per-file mappings remain authoritative. For episode-only messages such as `S01E03` or `עונה 1 פרק 3`, the group/channel name supplies the series title when the message and filename have no usable title. Private contact names are never treated as show titles. Existing episode-only entries are repaired from saved data without resetting scan progress; saved show identities are preserved.

Displayed titles prefer the first non-empty line of the Telegram message caption, with episode and release details removed. The filename is used when that line has no usable title (for example, a link or channel handle). Both the caption and filename remain searchable, and filename titles remain available for external metadata and AI title matching. Existing indexed titles are updated automatically on startup from saved captions, without scanning Telegram history again.

### ✨ AI search

Set `AI_SEARCH_ENABLED=true` and provide `GEMINI_API_KEY` to add separate **Telegram AI Movies** and **Telegram AI Series** catalogs. The existing **Telegram Videos** catalog keeps its ordinary text search. Reinstall or refresh the addon in your client after upgrading to discover the new catalogs.

By default, `REQUIRE_AI_SUFFIX_FOR_AI_SEARCH=true`: finish your description with a separate `AI` word when you are ready to search, for example `someone relives the same day AI`. The suffix is case insensitive (`AI`, `Ai`, `ai`, and `aI` all work). Queries without it return no AI results and make no Gemini request, preventing clients such as Nuvio from using API quota on unfinished text during pauses in typing. Trailing whitespace is ignored, and the suffix is removed before sending the description to Gemini; the description retains its original case. This applies to both AI catalogs and the Developer UI AI search form. Set `REQUIRE_AI_SUFFIX_FOR_AI_SEARCH=false` to allow AI searches without the suffix; a supplied suffix is still stripped.

Gemini uses Google Search to find up to five movie titles and five series titles matching the description. The addon searches those titles in the ordinary Telegram index and returns only normalized title matches in currently selected channels. The movie and series catalogs share one short-lived cached discovery response. Availability is checked locally on every search; category assignments come from Gemini and do not independently classify Telegram files. Results depend on online discovery and title naming, so aliases or translated filenames may not match.

No embeddings, description enrichment, or background AI indexing are generated. Only your search description is sent to Gemini; indexed titles, captions, Telegram credentials, channel IDs, playback URLs, and video bytes are not sent. Google Search grounding may have its own cost and quota in your Gemini project.

The Developer UI retains an AI search form with separate Movies and Series results.

If Gemini returns HTTP 429, the addon pauses Gemini requests with a saved, increasing cooldown that survives restarts. The Developer UI search displays failures and cooldown errors. The AI catalogs return no results during failures rather than substituting unrelated text matches; ordinary Telegram search remains available.

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

Save the output as `ADDON_ACCESS_KEY`.

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
ADDON_ACCESS_KEY=REPLACE_WITH_YOUR_GENERATED_ACCESS_KEY
TELEGRAM_APP_API_ID=123456
TELEGRAM_APP_API_HASH=YOUR_TELEGRAM_APP_API_HASH
USER_SESSION_STRING=YOUR_TELETHON_STRING_SESSION

DEV_UI_ENABLED=true
DEV_UI_HOST=0.0.0.0
# SKIP_DEV_UI_AUTH=false
CACHE_MB=512
# CHANNEL_IDS=-1001234567890,-1009876543210
```

When upgrading, rename `API_ID` to `TELEGRAM_APP_API_ID`, `API_HASH` to
`TELEGRAM_APP_API_HASH`, and `API_KEY` to `ADDON_ACCESS_KEY` in your configuration.
The credential values stay the same.

### 2. Build and start

The included `compose.yaml` is ready to use:

```yaml
services:
  addon:
    build: .
    env_file: .env
    environment:
      DEV_UI_HOST: ${DEV_UI_HOST:-0.0.0.0}
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
https://telegram.example.com/YOUR_ADDON_ACCESS_KEY/manifest.json
```

Initial indexing runs in the background. Hybrid searches can discover videos before their history has been indexed.

### 🔄 Update a Compose installation

From the repository directory:

```sh
git pull --ff-only
docker compose up --build -d
```

## 🏠 Install with Home Assistant

**You need:** a Home Assistant installation with the add-on store and an **amd64 / x86-64** or **aarch64 / ARM64** host (including Raspberry Pi 4/5 running 64-bit Home Assistant OS).

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
ADDON_ACCESS_KEY: "REPLACE_WITH_YOUR_GENERATED_ACCESS_KEY"
TELEGRAM_APP_API_ID: 123456
TELEGRAM_APP_API_HASH: "YOUR_TELEGRAM_APP_API_HASH"
USER_SESSION_STRING: "YOUR_TELETHON_STRING_SESSION"
developer_ui:
  DEV_UI_ENABLED: true
  SKIP_DEV_UI_AUTH: false
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
https://telegram.example.com/YOUR_ADDON_ACCESS_KEY/manifest.json
```

### 5. Open the dashboard

With `DEV_UI_ENABLED: true`, select **Open Web UI** on the add-on page and sign in with your `ADDON_ACCESS_KEY`. The Developer UI binds to `0.0.0.0` automatically in Home Assistant. Set `SKIP_DEV_UI_AUTH: true` under **Developer UI** to open it without signing in. Only enable this when access to the Developer UI listener is appropriately restricted; the setting also permits unauthenticated searches and sync requests. The add-on's `ADDON_ACCESS_KEY` remains required for Stremio endpoints.

The button uses the dashboard's mapped host port. You can also open `http://HOME_ASSISTANT_IP:8001` directly, using your chosen host port if you changed the mapping.

## 🧰 Developer UI

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
- **📋 Chats:** see which conversations are included in indexing and search.
- **🔎 Search:** inspect matching results and diagnostic details without playback.
- **👁️ Credentials:** reveal or hide the login password while entering it.
- **🔄 Sync now:** stop the current indexing cycle and start fresh channel discovery and catch-up indexing. Fully indexed channels and groups are checked for new content before unfinished history scans resume from saved progress. The dashboard shows **Syncing...** until indexing finishes, then **Sync done!**. Click **Sync now** again to restart a running sync. Failures appear in the dashboard, and sync progress is logged.
- **📝 Activity:** review recent addon activity.

For connection and indexing details, use the protected endpoint:

```text
https://telegram.example.com/YOUR_ADDON_ACCESS_KEY/status
```

The `/healthz` endpoint checks that the HTTP server is running; it does not confirm Telegram readiness.


Find it useful? You support is welcome!
https://buymeacoffee.com/hacstudio



### Telegram scan status and interval

`SCAN_INTERVAL_SECONDS` is a positive integer (default `300`): the delay after a
completed Telegram scan cycle before checking again. Set it in the environment
or Home Assistant options and restart. Initial scanning and confirmed history
backlogs continue promptly; **Sync now** bypasses the interval. Cycles never overlap.

In **Chats**, **Videos** is the number of indexed videos and **Queue**
estimates the messages still awaiting scanning using message ID ranges.
It includes deleted-message gaps (and can overestimate in basic groups with shared
message IDs), requires no extra Telegram requests, and reaches **0** when caught up.
**Checking…** means the scanner has not checked that channel yet.
Counts are refreshed by the scanner; refreshing the dashboard reads its current progress.

The Developer UI shows **Checking for updates** during discovery and backlog
checks, **Indexing** only after Telegram confirms unscanned messages, and
**Up to date** once all groups finish. **Scan incomplete** means a scan failed.
Unscanned messages may or may not contain videos. History checkpoints survive
restarts, and each catch-up uses a fixed latest-message target so new arrivals
are handled on the next cycle. TMDB matching runs independently of these states.


## ⚡ Indexing speed and playback priority

Chats are scanned **one at a time**, with fully indexed chats
refreshed before unfinished history scans. Background Telegram requests are
spaced by at least **1 second** after each response, including requests made
inside pagination and channel discovery. A flood wait pauses background
requests for Telegram's requested delay plus **1 second**, then doubles the
request spacing up to **5 seconds**. After every **20 successful requests**, the
spacing decreases by **0.25 seconds**, down to the 1-second baseline. These are
local pacing choices, not guaranteed Telegram limits; flood waits can still occur.
Sync now keeps the cooldown and pacing; restarting the add-on resets adaptive
spacing. Old `INDEXING_CONCURRENCY` values are ignored and can be removed.

History pages fetch up to 100 messages: 99 to index and one look-ahead message
that confirms whether unscanned history remains, avoiding a second request just
for look-ahead.

Indexing saves each page and its checkpoint in one database transaction.
Background scans, live searches, reconciliation, live-message indexing and automatic TMDB
matching pause during playback and resume after a **2-second grace period**
between player requests. Already-running requests or database batches may finish.
Playback startup and video downloads do not wait for the background scan limiter.
Continuous playback can delay indexing and leave new videos unavailable until playback stops.

