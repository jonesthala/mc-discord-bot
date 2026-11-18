"""Checks that a Minecraft name belongs to a real account before it goes on the whitelist.

When a server starts, the itzg image looks up every whitelisted name at playerdb.co and
refuses to start if one of them is unknown. So the bot asks the same service first, with
Mojang's API as a fallback when playerdb doesn't answer. Standard library only.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .core import McError, check_player
from .versions import USER_AGENT

log = logging.getLogger("mcbot.players")

PLAYERDB_URL = "https://playerdb.co/api/player/minecraft/{name}"
MOJANG_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"
MAX_BYTES = 100_000

Fetch = Callable[[str], tuple[int, Any]]
Lookup = Callable[[str], str | None]


class LookupUnavailable(Exception):
    """Neither service gave a clear answer (down, rate limited, unexpected reply)."""


def fetch_status(url: str, timeout: float = 5.0) -> tuple[int, Any]:
    """(HTTP status, parsed JSON or None). Error statuses are returned, not raised."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed hosts)
            status, raw = response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        with error:
            status, raw = error.code, error.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("response too large")
    try:
        return status, json.loads(raw) if raw.strip() else None
    except ValueError:
        return status, None


def _same_name(found: Any, asked: str) -> str:
    if isinstance(found, str) and found.lower() == asked.lower():
        return found
    raise LookupUnavailable(f"unexpected name {found!r} for {asked!r}")


def _read_playerdb(status: int, data: Any, name: str) -> str | None:
    body = data if isinstance(data, dict) else {}
    code = body.get("code")
    if status == 200 and code == "player.found":
        player = (body.get("data") or {}).get("player") or {}
        return _same_name(player.get("username"), name)
    if code == "minecraft.invalid_username":
        return None
    raise LookupUnavailable(f"playerdb answered HTTP {status} {code or ''}".strip())


def _read_mojang(status: int, data: Any, name: str) -> str | None:
    if status == 200 and isinstance(data, dict):
        return _same_name(data.get("name"), name)
    if status in (204, 404):  # 204 is what the API used to send for unknown names
        return None
    raise LookupUnavailable(f"Mojang answered HTTP {status}")


_SOURCES = ((PLAYERDB_URL, _read_playerdb), (MOJANG_URL, _read_mojang))


def lookup_player(name: str, fetch: Fetch = fetch_status) -> str | None:
    """The account's exact spelling, or None if Minecraft has no account with that name.

    Mojang is only asked when playerdb gives no clear answer: playerdb is what the server
    uses at start, so its "unknown" counts. Raises LookupUnavailable if neither answers.
    """
    problems = []
    for url, read in _SOURCES:
        try:
            status, data = fetch(url.format(name=urllib.parse.quote(name, safe="")))
            return read(status, data, name)
        except LookupUnavailable as exc:
            problems.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, broken JSON: try the next one
            problems.append(f"{urllib.parse.urlsplit(url).hostname}: {exc}")
    raise LookupUnavailable("; ".join(problems))


def check_account(raw: str, lookup: Lookup = lookup_player) -> tuple[str, bool]:
    """(name to store, verified). Refuses names without a Minecraft account. If no service
    can be reached, the typed name is kept unverified, so an outage never blocks the whitelist."""
    name = check_player(raw)
    try:
        found = lookup(name)
    except LookupUnavailable as exc:
        log.warning("Could not check the Minecraft name %s: %s", name, exc)
        return name, False
    if found is None:
        raise McError(
            f"Einen Minecraft-Account **{name}** gibt es nicht. Vertippt, oder hat die Person "
            "ihren Namen geändert? Ich habe nichts eingetragen."
        )
    return found, True
