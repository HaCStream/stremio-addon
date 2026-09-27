# Stremio Telegram for Home Assistant

This folder describes an amd64 Home Assistant add-on for the program in the
repository root. It uses `ghcr.io/home-assistant/amd64-base:3.24` and runs the
published image `ghcr.io/hilayc/stremio-addon:main`.

Home Assistant appends `version` to `image`, so `config.yaml` intentionally has
`image: ghcr.io/hilayc/stremio-addon` and `version: main`. Putting `:main` inside
the image field would cause Home Assistant to append a second tag.

## Build and publish

From the **repository root**, run:

```sh
docker build --platform linux/amd64 \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.24 \
  -f addon/Dockerfile \
  -t ghcr.io/hilayc/stremio-addon:main .
docker push ghcr.io/hilayc/stremio-addon:main
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
`https://github.com/hilayc/stremio-addon` in Home Assistant's add-on store
repository menu. Install **Stremio Telegram** on an amd64 installation.

Alternatively, copy this entire folder to `/addons/stremio-telegram` on
Home Assistant, reload the add-on store, and install the local entry. It still
pulls the published image. Local Supervisor source builds are not supported by
this folder alone: source builds require the repository-root context above.

## Configure

Set these options in the add-on's Configuration tab:

| Option / environment variable | Value |
| --- | --- |
| `PORT` | Unused host TCP port, default `8000` |
| `ADDON_URL` | External HTTPS base URL of your reverse proxy |
| `DEBUG_ENABLED` | Enable the read-only debug dashboard (default `true`) |
| `DEBUG_PORT` | Separate dashboard port (default `8001`) |
| `DEBUG_HOST` | Dashboard listen address (default `0.0.0.0`) |
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

Host networking lets the configured `PORT` work without a fixed Docker port
mapping. The server listens on all host interfaces, so choose a free port and
configure your network accordingly. No Supervisor or Home Assistant API access
is requested. Stremio needs a directly reachable endpoint.

Point your HTTPS reverse proxy at `http://HOME_ASSISTANT_IP:8000` (or your
configured port). Forward byte-range requests and disable proxy caching and
access logs that expose credential-bearing URLs. Install in Stremio with:

```text
https://YOUR_DOMAIN/YOUR_API_KEY/manifest.json
```

The protected `/<your-api-key>/status` endpoint shows Telegram indexing progress.
Optional AI search is configured with `AI_SEARCH_ENABLED`, `GEMINI_API_KEY`, and
`AI_SEARCH_PREFIX_ENABLED` under **AI search** in the Configuration tab. Debug
settings are grouped under **Debug dashboard**. Enabling AI adds a separate
Telegram AI Search catalog and starts background Gemini embedding calls. Prefix
mode restricts query-time calls to searches beginning with `AI` or `ai`; it does
not disable initial embedding calls. Titles and short caption excerpts are sent
to Gemini. The debug dashboard shows semantic indexing progress.
The debug dashboard is available directly on `DEBUG_PORT`. Sign in with the
configured API key to inspect channels, indexing progress, recent searches, and
read-only search results. It does not expose playback URLs and does not use
`ADDON_URL` for its own requests.
The add-on page's **Open Web UI** button opens the dashboard on its standard
port. Use **Sync now** in the dashboard after joining a channel or adding a video
to trigger channel discovery and catch-up indexing immediately.

SQLite and video chunks persist under `/data/stremio`; Supervisor keeps options
separately in `/data/options.json`. Restart the add-on after changing options.

The published image must run as root inside Home Assistant so it can read the
Supervisor-owned `/data/options.json` and write the add-on data directory. The
repository's root Dockerfile therefore does not set `USER addon`.

Reference: [Home Assistant add-on configuration](https://developers.home-assistant.io/docs/add-ons/configuration/).
