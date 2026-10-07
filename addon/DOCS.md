# Stremio Telegram for Home Assistant

This folder describes an amd64 Home Assistant add-on for the program in the
repository root. It uses `ghcr.io/home-assistant/amd64-base:3.24` and runs the
published image `ghcr.io/hacstream/stremio-addon:main`.

Home Assistant appends `version` to `image`, so `config.yaml` intentionally has
`image: ghcr.io/hacstream/stremio-addon` and `version: main`. Putting `:main` inside
the image field would cause Home Assistant to append a second tag.

## Build and publish

From the **repository root**, run:

```sh
docker build --platform linux/amd64 \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.24 \
  -f addon/Dockerfile \
  -t ghcr.io/hacstream/stremio-addon:main .
docker push ghcr.io/hacstream/stremio-addon:main
```

The final dot is important: the Dockerfile copies the existing `addon/`,
`requirements.txt`, and `generate_session.py` from the repository root.
Build automation must use `context: .` and
`file: addon/Dockerfile`. It must build this HA-specific image,
not the repository's original standalone Dockerfile, for the `main` tag.

Make the GHCR package public so Supervisor can pull it. Publishing is a separate
step; these files do not upload an image automatically. With a mutable `main`
tag, future pushes do not produce a new HA version notification; reinstall to
pull the replacement (back up the add-on first), or adopt versioned tags later.

## Install

After pushing these files to the repository and publishing the image, add
`https://github.com/HaCStream/stremio-addon` in Home Assistant's add-on store
repository menu. Install **Stremio Telegram** on an amd64 installation.

Alternatively, copy this entire folder to `/addons/stremio-telegram` on
Home Assistant, reload the add-on store, and install the local entry. It still
pulls the published image. Local Supervisor source builds are not supported by
this folder alone: source builds require the repository-root context above.

## Configure

Set these options in the add-on's Configuration tab:

| Option / environment variable | Value |
| --- | --- |
| `ADDON_URL` | External HTTPS base URL of your reverse proxy |
| `DEBUG_ENABLED` | Enable the read-only debug dashboard (default `true`) |
| `DEBUG_HOST` | Dashboard listen address (default `0.0.0.0`) |
| `SKIP_DEBUG_AUTH` | Open the dashboard without a key (default `false`); anyone with access can search and request a sync |
| `API_KEY` | At least 32 random URL-safe characters |
| `API_ID` | Positive Telegram application ID |
| `API_HASH` | Telegram application hash |
| `USER_SESSION_STRING` | Complete Telethon StringSession |
| `CACHE_MB` | Optional cache limit in MiB, default `512` |
| `CHANNEL_IDS` | Optional comma-separated negative channel IDs; blank scans all joined private channels |

For example, set `CHANNEL_IDS: "-1001234567890,-1009876543210"` and restart
the add-on. Only those joined private broadcast channels will be indexed.
Existing catalog entries from excluded channels are removed on discovery;
selecting them again restarts their history scan. Telegram posts are unchanged.

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
https://YOUR_DOMAIN/YOUR_API_KEY/manifest.json
```

The protected `/<your-api-key>/status` endpoint shows Telegram indexing progress.
Optional AI search is configured with `AI_SEARCH_ENABLED`, `GEMINI_API_KEY`,
`AI_SEARCH_PREFIX_ENABLED`, and `REQUIRE_DOT_SUFFIX_FOR_AI_SEARCH` under
**AI search** in the Configuration tab. Debug settings are grouped under **Debug dashboard**. Enabling AI adds separate
**Telegram AI Movies** and **Telegram AI Series** catalogs. Gemini uses Google
Search to find up to five titles per category, then the addon returns only titles
matched in the local Telegram index. Prefix mode requires a separate leading
`AI` word (case insensitive), including in the debug search form. Only the search
description is sent to Gemini; no indexed content or video data is sent.
`REQUIRE_DOT_SUFFIX_FOR_AI_SEARCH` defaults to `true`: finish your description
with a dot (`.`) to start AI search, for example `AI someone relives the same day.`
with prefix mode enabled. Until the final dot is present, no Gemini request is
made, avoiding unfinished searches and quota usage while typing in clients such
as Nuvio. The final dot is removed before sending the description. This applies
to both AI catalogs and debug AI search. Set the option to `false` to allow AI
searches without a final dot.
No background AI indexing or embeddings are generated. The debug search displays
separate movie and series matches. **Cleanup** removes legacy embeddings and
generated descriptions without removing Telegram entries, and reclaims database
space. The new catalogs may require reinstalling or refreshing the addon in your
client. Google Search grounding uses your Gemini project's quota and billing.
The debug dashboard is available through the host port mapped to 8001. Sign in with the
configured API key to inspect channels, indexing progress, recent searches, and
read-only search results. It does not expose playback URLs and does not use
`ADDON_URL` for its own requests.
Set `SKIP_DEBUG_AUTH: true` under the debug options to open without a key. Keep
dashboard network access restricted when using this option. The Stremio API key
is still required for addon endpoints.
The add-on page's **Open Web UI** button opens the dashboard on its standard
port by default. Use **Sync now** in the dashboard after joining a channel or adding a video
to trigger channel discovery and catch-up indexing immediately.

SQLite and video chunks persist under `/data/stremio`; Supervisor keeps options
separately in `/data/options.json`. Restart the add-on after changing options.

The published image must run as root inside Home Assistant so it can read the
Supervisor-owned `/data/options.json` and write the add-on data directory. The
repository's root Dockerfile therefore does not set `USER addon`.

Reference: [Home Assistant add-on configuration](https://developers.home-assistant.io/docs/add-ons/configuration/).
