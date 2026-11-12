"""Which Minecraft versions can be created, read from the Paper and Mojang APIs.

Lists are cached for an hour. Discord autocomplete has to answer within three seconds, so
callers can ask for the cached list even when it is stale; it is then refreshed in the
background. Standard library only.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from typing import Any, Callable

from .core import McError, normalize_version

log = logging.getLogger("mcbot.versions")

PAPER_URL = "https://fill.papermc.io/v3/projects/paper"
MOJANG_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
USER_AGENT = "mc-discord-bot/1.0 (+https://github.com/jonesthala/mc-discord-bot)"
CACHE_SECONDS = 3600
RETRY_SECONDS = 300
MAX_BYTES = 5_000_000
_RELEASE_RE = re.compile(r"^\d+(\.\d+){1,2}$")
_LATEST_WORDS = {"", "latest", "neueste", "newest", "neuste"}

Fetch = Callable[[str], Any]


def fetch_json(url: str, timeout: float = 6.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed URLs)
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("response too large")
    return json.loads(raw)


def paper_releases(data: Any) -> list[str]:
    """Stable Paper versions, newest first (the API lists them that way)."""
    groups = data.get("versions") if isinstance(data, dict) else None
    found: list[str] = []
    for group in groups.values() if isinstance(groups, dict) else []:
        for version in group if isinstance(group, list) else []:
            if isinstance(version, str) and _RELEASE_RE.match(version) and version not in found:
                found.append(version)
    return found


def vanilla_releases(data: Any) -> list[str]:
    """Release versions from Mojang's manifest, newest first."""
    entries = data.get("versions") if isinstance(data, dict) else None
    return [
        entry["id"]
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and entry.get("type") == "release" and isinstance(entry.get("id"), str)
    ]


SOURCES: dict[str, tuple[str, Callable[[Any], list[str]]]] = {
    "paper": (PAPER_URL, paper_releases),
    "vanilla": (MOJANG_URL, vanilla_releases),
}


class VersionCatalog:
    def __init__(self, fetch: Fetch = fetch_json, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._refreshing: set[str] = set()

    def _load(self, server_type: str) -> list[str]:
        url, parse = SOURCES[server_type]
        versions = parse(self._fetch(url))
        if not versions:
            raise ValueError("empty version list")
        return versions

    def _refresh(self, server_type: str) -> list[str]:
        with self._lock:
            old = self._cache.get(server_type)
        try:
            versions = self._load(server_type)
        except Exception as exc:  # noqa: BLE001 - the API being down must never break a command
            log.warning("Version list for %s not available: %s", server_type, exc)
            if old:  # keep using it, try again in a few minutes
                with self._lock:
                    self._cache[server_type] = (self._clock() - CACHE_SECONDS + RETRY_SECONDS, old[1])
                return old[1]
            return []
        with self._lock:
            self._cache[server_type] = (self._clock(), versions)
        return versions

    def releases(self, server_type: str, allow_stale: bool = False) -> list[str]:
        """Newest first; [] if the type is unknown or no list could be loaded yet."""
        if server_type not in SOURCES:
            return []
        with self._lock:
            hit = self._cache.get(server_type)
            fresh = hit is not None and self._clock() - hit[0] < CACHE_SECONDS
            if hit and not fresh and allow_stale:
                if server_type not in self._refreshing:
                    self._refreshing.add(server_type)
                    threading.Thread(target=self._background, args=(server_type,), daemon=True).start()
                return hit[1]
        if fresh and hit:
            return hit[1]
        return self._refresh(server_type)

    def _background(self, server_type: str) -> None:
        try:
            self._refresh(server_type)
        finally:
            with self._lock:
                self._refreshing.discard(server_type)

    def resolve(self, server_type: str, raw: str | None) -> str:
        """Turns the choice from Discord into a fixed version number."""
        text = (raw or "").strip()
        versions = self.releases(server_type)
        label = server_type.capitalize()
        if text.lower() in _LATEST_WORDS:
            if not versions:
                raise McError("Die Versionsliste ist gerade nicht erreichbar. Gib eine Nummer an, z.B. `1.21.4`.")
            return versions[0]
        version = normalize_version(text)
        if version in ("LATEST", "SNAPSHOT"):
            raise McError("Snapshots gibt es hier nicht. Wähle eine Version aus der Liste.")
        if versions and version not in versions:
            raise McError(
                f"{label} {version} gibt es nicht. Neueste: {versions[0]}. "
                "Beim Tippen zeigt Discord passende Versionen an."
            )
        return version

    def choices(self, server_type: str, current: str) -> list[tuple[str, str]]:
        """Up to 25 (label, value) pairs for Discord autocomplete, newest first."""
        versions = self.releases(server_type, allow_stale=True)
        needle = current.strip().lower()
        options: list[tuple[str, str]] = []
        if versions and (not needle or "neueste".startswith(needle) or "latest".startswith(needle)):
            options.append((f"Neueste: {versions[0]}", versions[0]))
        for version in versions:
            if needle and not version.startswith(needle):
                continue
            if options and options[0][1] == version:
                continue
            options.append((version, version))
            if len(options) == 25:
                break
        return options
