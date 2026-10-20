# Stremio Telegram for Home Assistant

The add-on supports **amd64 (x86-64)** and **aarch64 (ARM64)** hosts.
Home Assistant pulls `ghcr.io/hacstream/stremio-addon` using the version in
`config.yaml` and automatically selects the matching architecture.

## Build and publish

The release workflow publishes a multi-platform image from the repository-root
`Dockerfile` for `linux/amd64` and `linux/arm64`. See [RELEASING.md](../RELEASING.md)
for versioned releases. Adding an architecture to `config.yaml` requires a new
release containing that architecture before it can be installed.

For a manual HA base-image build, use the **repository root** as the context:

```sh
docker build --platform linux/arm64 \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base:3.24 \
  --build-arg BUILD_ARCH=aarch64 \
  -f addon/Dockerfile -t stremio-telegram:local .
```

For x86-64, use `linux/amd64`, `amd64-base`, and `BUILD_ARCH=amd64`.
`addon/build.yaml` lists the base images for both architectures. CI verifies
both this HA-specific Dockerfile and the published repository-root Dockerfile.

## Install

After publishing a release containing both architectures, add
`https://github.com/HaCStream/stremio-addon` in Home Assistant's add-on store
repository menu. Install **Stremio Telegram** on an amd64 or aarch64 installation.

Alternatively, copy this entire folder to `/addons/stremio-telegram` on
Home Assistant, reload the add-on store, and install the local entry. It still
pulls the published image. Local Supervisor source builds are not supported by
this folder alone: source builds require the repository-root context above.

## Configure

Set these options in the add-on's Configuration tab:

| Option / environment variable | Value |
| --- | --- |
| `ADDON_URL` | External HTTPS base URL of your reverse proxy |
| `DEV_UI_ENABLED` | Enable the read-only Developer UI (default `true`) |
| `SKIP_DEV_UI_AUTH` | Open the dashboard without a key (default `false`); anyone with access can search and refresh chats |
| `ADDON_ACCESS_KEY` | At least 32 random URL-safe characters |
| `TELEGRAM_APP_API_ID` | Positive Telegram application ID |
| `TELEGRAM_APP_API_HASH` | Telegram application hash |
| `USER_SESSION_STRING` | Complete Telethon StringSession |
| `DISPLAY_COLLECTION` | Show Telegram collections on the main screen (default `false`); search remains available. Restart and refresh/reinstall the addon in your client after changing it |
| `CACHE_MB` | Optional cache limit in MiB, default `512` |
| `CHANNEL_IDS` | Optional comma-separated chat IDs restricting search; blank searches all accessible account chats |

For example, set `CHANNEL_IDS: "-1001234567890,-1009876543210"` and restart
the add-on. Only those chats will be searched.
Existing catalog entries from excluded channels are removed on discovery;
selecting them again allows searches to discover videos. Telegram posts are unchanged.
Regular groups use negative IDs (for example, `-123456789`); supergroups and channels use `-100…` IDs. Private chats, bots and Saved Messages use positive user IDs. The `CHANNEL_IDS` option accepts all of these.

The settings use their exact uppercase names. The application
reads them directly from Home Assistant's `/data/options.json`; the HA startup
wrapper also exports them as environment variables. This makes both the generic
repository image and the HA-specific image work under Supervisor. Explicit
uppercase environment variables take precedence when running the image
outside Home Assistant.

Generate the session on a trusted machine using the repository's
`generate_session.py`. A Pyrogram/GramJS session or a `.session` filename is not
a Telethon StringSession. Startup validates the session format and reports a
clear error without printing the value. Telegram authorization is then checked
by the application.

The app listens on internal ports 8000 and 8001. Use the add-on's **Network**
section to change the host port mappings. No Supervisor or Home Assistant API
access is requested. Stremio needs a directly reachable endpoint.

Point your HTTPS reverse proxy at `http://HOME_ASSISTANT_IP:8000` (or your
mapped host port). Forward byte-range requests and disable proxy caching and
access logs that expose credential-bearing URLs. Install in Stremio with:

```text
https://YOUR_DOMAIN/YOUR_ADDON_ACCESS_KEY/manifest.json
```

The protected `/<your-api-key>/status` endpoint shows Telegram cached-video counts.
Files containing episode markers such as `S01E02`, `S01 E02`, or
`עונה 1 פרק 2` appear once per show in **Telegram Series**. Opening a show lists
its cached episodes in season/episode order; multiple files for the same episode
appear as separate playback choices. These files are excluded from **Telegram Videos**.
AI series results use the same grouping. Existing entries are updated automatically
on the first startup after upgrading, without a Telegram rescan.
Text search indexes the displayed title, original filename, and caption, so a
Hebrew caption and a Latin filename can both find the same entry. Joined/spaced
title variants such as `hashminia` and `Ha Shminia` are searchable too. Upgrading
rebuilds the local search index once to refresh older entries. This uses existing
text only; it does not translate or transliterate names automatically.
For shows opened from other catalogs using standard IMDb IDs (`tt...`), the addon
matches public English/Hebrew title aliases or explicit mappings and serves matching
episode streams. With a TMDB v3 API key configured under **TMDB show matching**, the background
matcher saves show-level TMDB/IMDb identities and also accepts `tmdb:ID:S:E`
and `tmdb:tv:ID:S:E` requests. Review ambiguous results or correct mappings in
the Developer UI's **TMDB matching** tab. Optional season/episode offsets
apply to all episodes of the selected show; arbitrary reorderings need separate
episode mapping and are not automated. Saved mappings survive restarts, apply
to new episodes, and require no TMDB call during playback. Blank keys disable
new lookups while retaining saved sources. Other ID formats are not matched.
Refresh or reinstall the addon in Nuvio/Stremio after upgrading so the client loads
the new series catalog and metadata capabilities. The client may still choose another
metadata provider for an external show; playback matching uses its requested season
and episode numbers.

Optional AI search is configured with `AI_SEARCH_ENABLED`, `GEMINI_API_KEY`,
and `REQUIRE_AI_SUFFIX_FOR_AI_SEARCH` under **AI search** in the Configuration
tab. Developer UI settings are grouped under **Developer UI**. Enabling AI adds
separate **Telegram AI Movies** and **Telegram AI Series** catalogs. Gemini uses
Google Search to find up to five titles per category, then the addon returns only
titles matched in the local Telegram index. Only the search description is sent
to Gemini; no cached content or video data is sent.
`REQUIRE_AI_SUFFIX_FOR_AI_SEARCH` defaults to `true`: finish your description
with a separate `AI` word to start AI search, for example
`someone relives the same day AI`. The suffix is case insensitive (`AI`, `Ai`,
`ai`, and `aI` all work). Until it is present, no Gemini request is made, avoiding
unfinished searches and quota usage while typing in clients such as Nuvio.
Trailing whitespace is ignored, and the suffix is removed before sending the
description. This applies to both AI catalogs and Developer UI AI search. Set the option
to `false` to allow AI searches without the suffix; a supplied suffix is still
stripped.
No background AI indexing or embeddings are generated. The Developer UI search displays
separate movie and series matches. The new catalogs may require reinstalling or refreshing the addon in your
client. Google Search grounding uses your Gemini project's quota and billing.
The Developer UI is available through the host port mapped to 8001. Sign in with the
`DEV_UI_PASSWORD` (or `ADDON_ACCESS_KEY` when the password is blank) to inspect channels, cached-video counts, recent searches, and
read-only search results. It does not expose playback URLs and does not use
`ADDON_URL` for its own requests.
Set `SKIP_DEV_UI_AUTH: true` under the Developer UI options to open without a password. Keep
dashboard network access restricted when using this option. The Stremio API key
is still required for addon endpoints.
The add-on page's **Open Web UI** button opens the dashboard on its standard
port by default. Use **Refresh chats** in the dashboard after joining a channel or group
to refresh chat identities without reading message history.

SQLite and video chunks persist under `/data/stremio`; Supervisor keeps options
separately in `/data/options.json`. Restart the add-on after changing options.

## AppArmor

Home Assistant loads `apparmor.txt` beside `config.yaml` automatically on hosts
with AppArmor support. The profile runs in enforcing mode, allows the Telegram,
metadata, AI, and HTTP connections, and supports both the published Python image
and the HA base image's s6 startup. It permits writes to `/data/stremio`, `/run`,
`/tmp`, and s6's configuration directory; application code and
`/data/options.json` remain read-only. No mount or host administration
permissions are granted. The profile applies to the Home Assistant add-on;
standalone Docker Compose keeps Docker's default profile.

Both images set `SQLITE_TMPDIR=/tmp` so large SQLite queries and database cleanup
can create temporary files in the permitted directory.

CI compiles the profile without loading it into the runner's kernel. Runtime
verification requires an AppArmor-enabled Home Assistant host: check startup,
chat discovery, playback/cache, Developer UI, enabled metadata/AI features, and
shutdown. Review host audit logs for denials with
`journalctl _TRANSPORT=audit -g 'apparmor="DENIED"'`. When diagnosing new image
requirements, temporarily add `complain` to the profile flags on a test host,
exercise those features, and review `apparmor="ALLOWED"` audit entries. Remove
`complain` before deploying the updated profile.

The published image must run as root inside Home Assistant so it can read the
Supervisor-owned `/data/options.json` and write the add-on data directory. The
repository's root Dockerfile therefore does not set `USER addon`.

Reference: [Home Assistant add-on configuration](https://developers.home-assistant.io/docs/add-ons/configuration/).



### On-demand search

Startup discovers chat identities without reading message history. Text searches, AI title searches and movie/episode source lookups query Telegram across history and save discovered videos locally. Queries are cached for 60 seconds and coalesced when identical requests overlap. Opening/reloading a series paginates searches only for that show: up to eight 100-message pages within twelve seconds per open. Videos and page cursors are saved atomically. Subsequent opens resume older pages, even after a restart; overlapping opens share a job. First pages refresh after 60 seconds without resetting older progress, and completed scopes are cached for 60 seconds. The account history is never crawled automatically. Collections and episode lists show cached discoveries and may be incomplete.

The Telegram phase has a 12-second budget and returns available cached/partial matches on failure. Searches share adaptive request pacing and yield to playback. Public metadata lookups may add time. Existing cached videos and manual mappings survive upgrades; source filtering still removes inaccessible/excluded entries on discovery.

The Developer UI's **Refresh chats** reloads source identities without scanning messages. Chats refresh automatically at startup and on a search after their five-minute identity cache expires. There is no periodic history scan or reconciliation. Edits/deletions update cached entries when events arrive, and playback revalidates files. Missed events can leave stale cached entries.

`SCAN_INTERVAL_SECONDS` and `INDEXING_CONCURRENCY` are obsolete and ignored. Remove them from existing option/environment files. Cached-video counts replace indexing/queue progress. Automatic TMDB matching processes only user-requested shows, not the entire cache.
