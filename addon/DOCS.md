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
| `SKIP_DEV_UI_AUTH` | Open the dashboard without a key (default `false`); anyone with access can search and request a sync |
| `ADDON_ACCESS_KEY` | At least 32 random URL-safe characters |
| `TELEGRAM_APP_API_ID` | Positive Telegram application ID |
| `TELEGRAM_APP_API_HASH` | Telegram application hash |
| `USER_SESSION_STRING` | Complete Telethon StringSession |
| `DISPLAY_COLLECTION` | Show Telegram collections on the main screen (default `false`); search remains available. Restart and refresh/reinstall the addon in your client after changing it |
| `CACHE_MB` | Optional cache limit in MiB, default `512` |
| `CHANNEL_IDS` | Optional comma-separated chat IDs restricting indexing and live search; blank scans all accessible account chats |

For example, set `CHANNEL_IDS: "-1001234567890,-1009876543210"` and restart
the add-on. Only those chats will be indexed and searched live.
Existing catalog entries from excluded channels are removed on discovery;
selecting them again restarts their history scan. Telegram posts are unchanged.
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

The protected `/<your-api-key>/status` endpoint shows Telegram indexing progress.
Files containing episode markers such as `S01E02`, `S01 E02`, or
`עונה 1 פרק 2` appear once per show in **Telegram Series**. Opening a show lists
its indexed episodes in season/episode order; multiple files for the same episode
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
to Gemini; no indexed content or video data is sent.
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
`DEV_UI_PASSWORD` (or `ADDON_ACCESS_KEY` when the password is blank) to inspect channels, indexing progress, recent searches, and
read-only search results. It does not expose playback URLs and does not use
`ADDON_URL` for its own requests.
Set `SKIP_DEV_UI_AUTH: true` under the Developer UI options to open without a password. Keep
dashboard network access restricted when using this option. The Stremio API key
is still required for addon endpoints.
The add-on page's **Open Web UI** button opens the dashboard on its standard
port by default. Use **Sync now** in the dashboard after joining a channel or group or adding a video
to trigger channel discovery and catch-up indexing immediately.

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
channel sync, playback/cache, Developer UI, enabled metadata/AI features, and
shutdown. Review host audit logs for denials with
`journalctl _TRANSPORT=audit -g 'apparmor="DENIED"'`. When diagnosing new image
requirements, temporarily add `complain` to the profile flags on a test host,
exercise those features, and review `apparmor="ALLOWED"` audit entries. Remove
`complain` before deploying the updated profile.

The published image must run as root inside Home Assistant so it can read the
Supervisor-owned `/data/options.json` and write the add-on data directory. The
repository's root Dockerfile therefore does not set `USER addon`.

Reference: [Home Assistant add-on configuration](https://developers.home-assistant.io/docs/add-ons/configuration/).



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


### Hybrid search and account coverage

With `CHANNEL_IDS` blank, indexing covers accessible channels, groups, private chats, bots, Saved Messages and archived dialogs. It examines history for video attachments without processing tag replies. Text searches and movie/episode source lookups also query Telegram, including when the index is complete. Identical successful queries are cached for 60 seconds; concurrent identical requests share one query. Matches are persisted without advancing scan checkpoints and deduplicated by chat/message ID. Live searches use the background request limiter, pause for playback and have a 12-second phase budget; failures return available local/partial results.

Unfiltered searches inspect up to three pages of 100 messages, collecting up to 100 videos per query; filtered searches query selected chats directly. Movie matching accepts strong titles with missing years and conservative subtitle variants, ranks matching years first and rejects known conflicting years. Episode-only messages can use a group/channel title as their series name. Contact names are not series titles. Saved show mappings and offsets remain authoritative. See the main README for details and limits.


## Catalog indexing and live search

Background indexing builds browsable catalogs and grouped episode lists. Searches
also query Telegram, even when the local index already has matches or the chat is
fully indexed. A newly uploaded episode can therefore be found before the next
indexing cycle. Public-catalog episode playback searches for that specific episode;
it does not wait for the full series to be indexed.

`CATALOG_INDEXING_ENABLED` defaults to `true`. Keep it enabled for complete local
catalogs over time. Set it to `false` if you mainly use search or external catalogs
and want to reduce Telegram requests. Disabling it stops automatic history scans,
catch-up scans and reconciliation; it keeps existing videos/checkpoints, chat
discovery, incoming message updates and on-demand search. Sync now refreshes chats
only in this mode. Re-enable it to resume saved indexing progress after restart.
`DISPLAY_COLLECTION` controls catalog visibility separately.

A single indexing worker resumes history from persistent checkpoints, then checks
newer messages on later cycles. Live searches take priority over queued indexing
requests; an RPC already in progress finishes first. Both share request pacing and
Telegram cooldowns, and both pause during playback. Search traffic can consequently
slow indexing. Catalogs and episode lists may be incomplete until indexing catches
up, and a client that cached an older list may need to refresh it. No full-history
series search is started when opening a show.
