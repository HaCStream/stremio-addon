"""Translate Supervisor options to the application's environment, without logging secrets."""
import json
import logging
import os
import sys
from pathlib import Path

FIELDS = (
    "ADDON_URL", "API_KEY", "API_ID", "API_HASH",
    "USER_SESSION_STRING", "CACHE_MB", "CHANNEL_IDS",
    "DEBUG_HOST", "DEBUG_ENABLED", "SKIP_DEBUG_AUTH", "AI_SEARCH_ENABLED", "GEMINI_API_KEY",
    "REQUIRE_AI_SUFFIX_FOR_AI_SEARCH", "TMDB_API_KEY",
)
GROUPS = {name: "debug" for name in ("DEBUG_HOST", "DEBUG_ENABLED", "SKIP_DEBUG_AUTH")}
GROUPS.update({name: "ai" for name in ("AI_SEARCH_ENABLED", "GEMINI_API_KEY",
                                      "REQUIRE_AI_SUFFIX_FOR_AI_SEARCH")})

GROUPS["TMDB_API_KEY"] = "tmdb"


def configure(options, environ):
    for name in FIELDS:
        value = options.get(name, options.get(name.lower()))
        group = options.get(GROUPS.get(name))
        if isinstance(group, dict):
            value = group.get(name, group.get(name.lower(), value))
        if value is not None and name not in environ:
            environ[name] = str(value)
    # Supervisor owns /data/options.json; keep app files in their own directory.
    environ["DATA_DIR"] = "/data/stremio"


def main():
    options_path = Path("/data/options.json")
    try:
        options = json.loads(options_path.read_text()) if options_path.exists() else {}
        if not isinstance(options, dict):
            raise ValueError()
    except (OSError, ValueError):
        sys.exit("Cannot read Home Assistant options: expected a JSON object in /data/options.json.")
    configure(options, os.environ)

    from addon.core import Settings
    from telethon.sessions import StringSession

    try:
        settings = Settings.env()
    except ValueError as exc:
        sys.exit(str(exc))
    try:
        session = StringSession(settings.session)
        if not session.auth_key:
            raise ValueError()
    except Exception:
        sys.exit(
            "Invalid user_session_string. Generate a Telethon StringSession using "
            "generate_session.py, then paste only its complete output value into "
            "the add-on's user_session_string option."
        )
    debug = f" and Developer UI on {settings.debug_host}:{settings.debug_port}" if settings.debug_enabled else ""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s: %(name)s: %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    logging.getLogger(__name__).info("Starting Stremio Telegram on port %s%s", settings.port, debug)
    os.execv(sys.executable, [sys.executable, "-m", "addon"])


if __name__ == "__main__":
    main()
