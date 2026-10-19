# 📺 Stremio Telegram

Watch videos from Telegram channels and groups you have joined in **Stremio or Nuvio**, using your Home Assistant server. The add-on indexes joined public and private channels and groups, groups series by episode, and streams files without transcoding.

## 🚀 Get started

1. Add `https://github.com/HaCStream/stremio-addon` as a repository in the Home Assistant **Add-on Store**, then install **Stremio Telegram**. An **amd64 (x86-64)** or **aarch64 (ARM64)** Home Assistant host is required.
2. Get your Telegram **API ID** and **API hash** from [my.telegram.org](https://my.telegram.org). Run the repository's [`generate_session.py`](https://github.com/HaCStream/stremio-addon/blob/main/generate_session.py) on a trusted computer to create a Telethon session string.
3. In the add-on's **Configuration** tab, set `TELEGRAM_APP_API_ID`, `TELEGRAM_APP_API_HASH`, `USER_SESSION_STRING`, `ADDON_ACCESS_KEY` (at least 32 URL-safe characters), and `ADDON_URL` (the public HTTPS address of this add-on).
4. Start the add-on. Point your HTTPS reverse proxy at port **8000** on your Home Assistant host, then install this URL in Stremio or Nuvio:

   ```text
   https://YOUR_DOMAIN/YOUR_ADDON_ACCESS_KEY/manifest.json
   ```

🔐 Keep your session string, access key, and installation URL private. Stremio/Nuvio must be able to reach `ADDON_URL` directly; Home Assistant ingress alone is not enough for playback.

## ⚙️ Useful options

| Option | What it does |
| --- | --- |
| `CHANNEL_IDS` | Leave empty to index all joined channels and groups, or enter comma-separated channel or group IDs to limit the selection. |
| `CACHE_MB` | Sets the video chunk cache limit in MiB (default: `512`). |
| **TMDB show matching** → `TMDB_API_KEY` | Matches Telegram series to public catalog episodes; review uncertain matches in the Developer UI. |
| **AI search** → `AI_SEARCH_ENABLED`, `GEMINI_API_KEY` | Adds AI movie and series search. By default, end the search with `AI` to send it to Gemini. |
| **Developer UI** → `DEV_UI_ENABLED` | Enables the dashboard for channel status, manual sync, search, and TMDB matching. Open it through the add-on's **Open Web UI** button. |

🔄 After enabling new catalogs or TMDB matching, refresh or reinstall the add-on in Stremio/Nuvio. Restart the Home Assistant add-on after changing its options.

For all settings and reverse proxy details, see the [configuration guide](DOCS.md).


## ⚡ Indexing speed and playback priority

Channels and groups are scanned **one at a time**, with fully indexed chats
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
Background scans, reconciliation, live-message indexing and automatic TMDB
matching pause during playback and resume after a **2-second grace period**
between player requests. Already-running requests or database batches may finish.
Playback startup and video downloads do not wait for the background scan limiter.
Continuous playback can delay indexing and leave new videos unavailable until playback stops.


🔄 **Sync now** restarts any running indexing cycle, keeping saved progress. Fully indexed channels and groups are refreshed first, then unfinished history scans resume. You can click it again while syncing to restart. Streaming keeps priority over indexing.
