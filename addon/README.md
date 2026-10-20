# 📺 Stremio Telegram

Watch videos from your Telegram account in **Stremio or Nuvio**, using your Home Assistant server. The add-on searches channels, groups, private chats, bots and Saved Messages on demand, saves discovered videos locally, groups episodes and streams without transcoding. There is no full-history indexing.

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
| `CHANNEL_IDS` | Restricts search to comma-separated chat IDs. Leave empty for all accessible account chats. |
| `CACHE_MB` | Sets the video chunk cache limit in MiB (default: `512`). |
| **TMDB show matching** → `TMDB_API_KEY` | Matches Telegram series to public catalog episodes; review uncertain matches in the Developer UI. |
| **AI search** → `AI_SEARCH_ENABLED`, `GEMINI_API_KEY` | Adds AI movie and series search. By default, end the search with `AI` to send it to Gemini. |
| **Developer UI** → `DEV_UI_ENABLED` | Enables the dashboard for chat status, chat refresh, search, and TMDB matching. Open it through the add-on's **Open Web UI** button. |

🔄 After enabling new catalogs or TMDB matching, refresh or reinstall the add-on in Stremio/Nuvio. Restart the Home Assistant add-on after changing its options.

For all settings and reverse proxy details, see the [configuration guide](DOCS.md).


## 🔎 Search on demand

Searches query Telegram across history and save discovered videos. Identical queries are cached for **60 seconds**; simultaneous duplicates share one request. Opening/reloading a series loads up to **eight 100-message pages within twelve seconds** for that show. Page cursors and discoveries are saved together; reopen to continue, including after a restart. Duplicate opens share a job, and recent first pages refresh after 60 seconds while retaining older progress. No account-wide crawl runs. Collections and episode lists contain discovered items and may be incomplete. Existing cached videos and manual mappings survive upgrades.

Telegram searches have a **12-second phase budget** and return available cached/partial results on a timeout or failure. Movie matching accepts strong missing-year titles; episode-only videos can use a group/channel name as their series title. Saved mappings remain authoritative.

The **Chats** tab shows cached video counts, not indexing progress. **Refresh chats** reloads chat identities without scanning history. Private/bot/Saved Messages IDs are positive; regular groups are negative; channels/supergroups use `-100…` IDs. `CHANNEL_IDS` restricts searches. Tag replies are not interpreted.

## ⚡ Playback priority

Search/discovery requests are paced at least one second apart. Flood waits increase spacing up to five seconds, with gradual recovery. Searches and requested-show metadata matching yield to playback; the player bypasses the search limiter. Already-running requests may finish.

`SCAN_INTERVAL_SECONDS` and `INDEXING_CONCURRENCY` are obsolete and ignored. No periodic history scans run. Missed edits/deletions may remain cached until rediscovery or playback validation.
