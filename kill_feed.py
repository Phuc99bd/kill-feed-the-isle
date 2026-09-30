#!/usr/bin/env python3
"""One-shot kill-feed updater: fetch API rows and edit a Discord message embed.

Compatible with Python 3.6+ (CentOS 7 system Python).
"""

from __future__ import print_function

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

API_URL = "https://sbtcislandd.com/api/boards/species"
DISCORD_API_BASE = "https://discord.com/api/v10"
HTTP_TIMEOUT = 10
MAX_RETRIES = 2
BACKOFF_SECONDS = 1.0
EMBED_DESCRIPTION_LIMIT = 4096

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("kill_feed")


def fetch_kill_feed(species):
    # type: (str) -> Dict[str, Any]
    """GET the species kill-feed board. Retries transient network errors."""
    params = {"species": species}
    last_error = None  # type: Optional[Exception]

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(API_URL, params=params, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Kill-feed API returned non-object JSON")
            return data
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Kill-feed fetch attempt %s failed (%s); retrying in %.1fs",
                    attempt + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                break
        except requests.HTTPError as exc:
            logger.error(
                "Kill-feed API HTTP error: status=%s body=%s",
                exc.response.status_code if exc.response is not None else "?",
                (exc.response.text[:200] if exc.response is not None else ""),
            )
            raise

    assert last_error is not None
    raise last_error


def _growth_label(growth):
    # type: (Any) -> str
    if growth is None:
        return "?"
    return "{}%".format(int(growth))


def _format_row(row):
    # type: (Dict[str, Any]) -> str
    at = int(row.get("at") or 0)
    ts = "<t:{}:R>".format(at)
    victim_name = row.get("victim_name") or "Unknown"
    victim_species = row.get("victim_species") or "?"
    victim_growth = _growth_label(row.get("victim_growth"))

    killer_known = bool(row.get("killer_known"))
    cause = (row.get("cause") or "").lower()

    if killer_known and cause == "pvp":
        killer_name = row.get("killer_name") or "Unknown"
        killer_species = row.get("killer_species") or "?"
        killer_growth = _growth_label(row.get("killer_growth"))
        return (
            "🩸 **{killer}** `{ks}` ({kg}) "
            "đã hạ gục **{victim}** `{vs}` ({vg}) • {ts}"
        ).format(
            killer=killer_name,
            ks=killer_species,
            kg=killer_growth,
            victim=victim_name,
            vs=victim_species,
            vg=victim_growth,
            ts=ts,
        )

    return (
        "💀 **{victim}** `{vs}` ({vg}) chết tự nhiên • {ts}"
    ).format(
        victim=victim_name,
        vs=victim_species,
        vg=victim_growth,
        ts=ts,
    )


def format_kill_feed(data, max_rows):
    # type: (Dict[str, Any], int) -> Dict[str, Any]
    """Pure function: build a Discord embed dict from API payload."""
    species = data.get("species") or "Unknown"
    title = "Kill Feed — {}".format(species)

    if not data.get("ok") or not data.get("available") or data.get("gated"):
        reason = data.get("reason") or "unavailable"
        return {
            "title": title,
            "description": "Feed unavailable (`{}`).".format(reason),
            "color": 0x808080,
        }

    rows = data.get("rows") or []
    if not isinstance(rows, list):
        rows = []

    shown = rows[:max_rows]
    lines = [_format_row(row) for row in shown if isinstance(row, dict)]

    remaining = len(rows) - len(shown)
    if remaining > 0 or data.get("more"):
        extra = remaining if remaining > 0 else 0
        if data.get("more") and remaining <= 0:
            lines.append("... and more")
        elif extra > 0:
            lines.append("... and {} more".format(extra))

    description = "\n".join(lines) if lines else "_No recent kills._"
    if len(description) > EMBED_DESCRIPTION_LIMIT:
        description = (
            description[: EMBED_DESCRIPTION_LIMIT - 20].rstrip() + "\n... truncated"
        )

    return {
        "title": title,
        "description": description,
        "color": 0xE74C3C,
    }


def edit_discord_message(token, channel_id, message_id, embed):
    # type: (str, str, str, Dict[str, Any]) -> None
    """PATCH an existing Discord message with a new embed."""
    url = "{}/channels/{}/messages/{}".format(
        DISCORD_API_BASE, channel_id, message_id
    )
    headers = {
        "Authorization": "Bot {}".format(token),
        "Content-Type": "application/json",
    }
    payload = {"embeds": [embed]}

    response = requests.patch(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)

    if response.status_code == 401:
        raise RuntimeError("Discord 401: bad bot token")
    if response.status_code == 403:
        raise RuntimeError(
            "Discord 403: missing permissions (need View Channel + Send/Manage Messages)"
        )
    if response.status_code == 404:
        raise RuntimeError("Discord 404: bad CHANNEL_ID or MESSAGE_ID")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            try:
                retry_after = response.json().get("retry_after", "?")
            except Exception:
                retry_after = "?"
        raise RuntimeError(
            "Discord 429: rate limited; Retry-After={}".format(retry_after)
        )

    if not response.ok:
        raise RuntimeError(
            "Discord HTTP {}: {}".format(response.status_code, response.text[:300])
        )


def _require_env(name):
    # type: (str) -> str
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit("Missing required env var: {}".format(name))
    return value


def run_once(token, channel_id, message_id, species, max_rows):
    # type: (str, str, str, str, int) -> None
    logger.info("Fetching kill-feed for species=%s", species)
    data = fetch_kill_feed(species)
    embed = format_kill_feed(data, max_rows=max_rows)
    row_count = len((data.get("rows") or [])[:max_rows])
    logger.info("Formatted embed with up to %s rows (showing %s)", max_rows, row_count)
    edit_discord_message(token, channel_id, message_id, embed)
    logger.info("Updated message %s in channel %s", message_id, channel_id)


def main():
    # type: () -> int
    parser = argparse.ArgumentParser(description="Update Discord kill-feed message")
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        help="Seconds between updates (0 = run once). Example: --interval 30",
    )
    args = parser.parse_args()

    load_dotenv()

    token = _require_env("DISCORD_BOT_TOKEN")
    channel_id = _require_env("CHANNEL_ID")
    message_id = _require_env("MESSAGE_ID")
    species = os.getenv("SPECIES", "Deinosuchus").strip() or "Deinosuchus"
    max_rows_raw = os.getenv("MAX_ROWS", "30").strip() or "30"
    try:
        max_rows = max(1, int(max_rows_raw))
    except ValueError:
        logger.error("MAX_ROWS must be an integer, got %r", max_rows_raw)
        return 1

    if args.interval < 0:
        logger.error("--interval must be >= 0")
        return 1

    if args.interval == 0:
        try:
            run_once(token, channel_id, message_id, species, max_rows)
        except Exception:
            logger.exception("Failed to update kill-feed")
            return 1
        return 0

    logger.info("Looping every %ss (Ctrl+C to stop)", args.interval)
    while True:
        try:
            run_once(token, channel_id, message_id, species, max_rows)
        except Exception:
            logger.exception("Update failed; will retry after interval")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            return 0


if __name__ == "__main__":
    sys.exit(main())
