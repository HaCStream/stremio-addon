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
| `INDEXING_CONCURRENCY` | Number of Telegram channels or groups scanned concurrently. |
| **TMDB show matching** → `TMDB_API_KEY` | Matches Telegram series to public catalog episodes; review uncertain matches in the Developer UI. |
| **AI search** → `AI_SEARCH_ENABLED`, `GEMINI_API_KEY` | Adds AI movie and series search. By default, end the search with `AI` to send it to Gemini. |
| **Developer UI** → `DEV_UI_ENABLED` | Enables the dashboard for channel status, manual sync, search, and TMDB matching. Open it through the add-on's **Open Web UI** button. |

🔄 After enabling new catalogs or TMDB matching, refresh or reinstall the add-on in Stremio/Nuvio. Restart the Home Assistant add-on after changing its options.

For all settings and reverse proxy details, see the [configuration guide](DOCS.md).


## ⚡ Indexing speed and playback priority

`INDEXING_CONCURRENCY` defaults to **2**, with an enforced range of **1–4**.
Keep **2** for normal use. Try **3–4** when an initial import spans many channels
and the host has spare CPU, memory, disk and network capacity. Use **1** on a
resource-constrained host or if Telegram flood waits become frequent. A higher
value overlaps channel requests; it does not accelerate a single channel, bypass
Telegram limits, or create additional database writers. More concurrency uses
more resources and can trigger longer flood waits, making indexing slower.
Restart the add-on after changing it.

Indexing saves each page and its checkpoint in one database transaction.
Background scans, reconciliation, live-message indexing and automatic TMDB
matching pause during playback and resume after a **2-second grace period**
between player requests. Already-running requests or database batches may finish.
Playback startup and video downloads do not wait for the background scan limiter.
The pause applies at every concurrency setting; continuous playback can delay
indexing and leave new videos unavailable until playback stops.
