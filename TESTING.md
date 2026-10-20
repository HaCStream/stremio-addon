# Validation

Run `python -m pytest -q` and `python scripts/validate_addon_config.py`.
The tests use mocked Telegram responses; live-account and container acceptance are separate checks.

1. Start with a valid user session. Verify chat discovery reaches **Search on demand** without fetching message history, even when an old database contains unfinished checkpoints.
2. Search known titles in channels, groups, private/bot chats and Saved Messages. Verify `CHANNEL_IDS` restricts results and playback. Repeated queries should reuse cached results.
3. Open a discovered series and verify additional episodes are fetched on demand. Confirm manual identities and episode offsets remain authoritative.
4. Search with AI from an empty cache. Confirm suggested titles are searched on Telegram before local title matching.
5. Check **Refresh chats** updates the source list without scanning videos. Cached counts must not imply a complete library or show an indexing queue.
6. Play and seek MP4/large files, verify Range/HEAD responses and bounded chunk caching. Search/discovery must yield to streaming and respect flood waits.
7. Restart and verify cached discoveries and mappings remain available. Edit/delete cached files and check event updates/playback validation; missed events may leave stale cache entries.
8. Verify failures return cached/partial matches and sanitized diagnostics. Complete episode lists and every available source are not guaranteed by bounded on-demand searches.
