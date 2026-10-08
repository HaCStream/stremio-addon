# Search and result logs

Interaction summaries are enabled by default at INFO level and appear in the
Home Assistant add-on log or `docker compose logs -f addon`.

Every application log entry, including server and Home Assistant startup logs,
is prefixed with the date, time, and UTC offset in the runtime's local timezone.
For example: `2026-10-08T12:28:25+0300 INFO: addon.app: ...`.
Containers using UTC display `+0000`.

Each catalog search/browse, metadata lookup, and source lookup emits one JSON
summary with its event, query or item ID, HTTP status and duration in milliseconds.
Successful lookups include the returned result count, distinct channel count,
and up to three result titles. Counts refer to the current response/page, not
the total number of indexed videos. Source summaries identify direct Telegram
ID lookups versus metadata matching.

Example:

```json
{"event":"catalog_search","query":"שם הסרט","type":"movie","catalog":"telegram","result_count":2,"skip":0,"channel_count":1,"sample_titles":["שם הסרט","שם הסרט"],"status":200,"duration_ms":3.2}
```

Zero results are logged too. HTTP errors use WARNING level. Unexpected failures
record the exception class without the exception message in the interaction
summary. Health checks and unauthorized requests do not emit interaction logs.

Queries and sample titles are private viewing information, so review logs before
sharing them. Text fields are limited to 200 characters and control/direction
characters are replaced with spaces. Known API keys, session strings, API hashes,
HTTP URLs, and playback/thumbnail paths are redacted. Raw request URLs, captions,
headers and stream-response bodies are not included. HTTP access logs remain off.
