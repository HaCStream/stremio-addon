# 📺 Stremio Telegram

Watch videos from your Telegram account in **Stremio or Nuvio**, using your Home Assistant server. The add-on indexes channels, groups, private chats, bots and Saved Messages, groups series by episode, and streams files without transcoding.

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
| `CHANNEL_IDS` | Restricts indexing and live search to comma-separated chat IDs. Leave empty for all accessible account chats. |
| `CACHE_MB` | Sets the video chunk cache limit in MiB (default: `512`). |
| **TMDB show matching** → `TMDB_API_KEY` | Matches Telegram series to public catalog episodes; review uncertain matches in the Developer UI. |
| **AI search** → `AI_SEARCH_ENABLED`, `GEMINI_API_KEY` | Adds AI movie and series search. By default, end the search with `AI` to send it to Gemini. |
| **Developer UI** → `DEV_UI_ENABLED` | Enables the dashboard for chat status, manual sync, search, and TMDB matching. Open it through the add-on's **Open Web UI** button. |

🔄 After enabling new catalogs or TMDB matching, refresh or reinstall the add-on in Stremio/Nuvio. Restart the Home Assistant add-on after changing its options.

For all settings and reverse proxy details, see the [configuration guide](DOCS.md).


## 🔎 Search before indexing finishes

Search combines your index with Telegram on demand, including after a completed scan. Results are merged without duplicating files; live hits do not mark a chat fully indexed. Identical Telegram queries are cached for **60 seconds**, so new uploads can take up to that long to appear. Live searches share indexing's request limiter and yield to playback. After a **12-second live-search budget** or upstream failure, available local/partial results are returned.

Movie matching accepts strong titles without a year, ranks exact title/year matches first and avoids conflicting release years. Episode-only videos can inherit their series name from the group/channel title; manual mappings stay authoritative.

The **Chats** tab lists source IDs: positive IDs for private/bot/Saved Messages conversations, negative IDs for regular groups and `-100…` IDs for channels/supergroups. `CHANNEL_IDS` restricts both indexing and live search. Only video attachments are indexed; tag replies are not interpreted.

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


🔄 **Sync now** restarts any running indexing cycle, keeping saved progress. Fully indexed channels and groups are refreshed first, then unfinished history scans resume. You can click it again while syncing to restart. Streaming keeps priority over indexing.


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

## 💾 Local downloads

In the Developer UI, **Search** includes download, pause, resume and stop/delete
controls beside each video's caption, file name, group/channel and size.
**Downloads** lists saved and unfinished files, supports selecting files for batch
deletion, and links back to Search. AI search diagnostics are in **AI debug**.

Videos are saved to Home Assistant's shared **`/media/telegram-downloads`**, not to
your phone or browser. The add-on maps `/media` writable and its AppArmor profile
allows writes to that subdirectory. Keep protection mode and AppArmor enabled.
Use a separately configured file share to access files from another computer.

There is no app-imposed concurrent-download limit. Telegram flood waits are
respected, and downloads yield between chunks to playback. Streaming may still
slow down while downloads are in progress. Files use `.part` until complete;
**Stop & delete** removes the partial file. Closing the UI keeps downloads running.
After restarting the add-on, unfinished jobs appear paused: **Resume** continues
from the partial file. Low disk space or access errors show a failed status;
fix the cause and resume. Keep the add-on data and media files to retain resume
state. Only downloads created by this feature are managed by this screen.
