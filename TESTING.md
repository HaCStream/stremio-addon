# Validation

Automated result: **34 passed** on Python 3.12. The only warning was a
third-party Starlette/AnyIO deprecation. Docker and live Telegram/Stremio
acceptance were not run in this environment.

The unit/integration tests use fake Telegram responses; they do not establish
live Telegram connectivity or real Stremio player compatibility.

Before exposing this as your daily source:

1. Start with an authorized dedicated user session and verify the protected
   status endpoint transitions to indexing/ready and lists expected channels.
2. Verify a Hebrew caption search, niqqud variation, English title, and mixed
   title find the intended uploads. Joined public and private broadcast channels should appear; groups and
   supergroups should not appear.
3. Play a small MP4 on your Stremio device, then a multi-gigabyte file. Seek to
   the middle and near the end; pause/resume and stop. Check memory remains
   bounded and the chunk directory stays within the configured cache ceiling.
4. Check a known movie with an explicit IMDb ID and a Hebrew title with a
   Wikidata alias. Verify wrong years/episodes produce no inferred sources.
5. Restart during history scanning and confirm progress resumes. Post/edit/delete
   a test video and verify the catalog updates. Revoke channel access and verify
   playback fails, including an already issued URL.
6. Test metadata-provider downtime: the Telegram catalog and explicitly mapped
   sources should still work (a metadata timeout may delay stream responses).
7. Check your proxy forwards HEAD and Range, uses HTTPS, avoids shared caching,
   and does not record API keys or signed playback URLs in access logs.
8. Revoke the dedicated Telegram session and verify status indicates the error;
   generate a replacement and restart. Do not use your main device's session.

Known scope limits: direct playback only; no grouped Telegram-series catalog;
no fuzzy title guesses; no management UI; no automatic session re-login. Old
edits/deletions reconcile gradually. Huge channel histories and Telegram flood
limits determine initial indexing time and streaming speed.
