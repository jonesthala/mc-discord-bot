"""What the admin API answers, built from the bot's own data. Standard library only, so it can be tested anywhere."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

MAX_LOG_LINES = 1000
_MARKUP_RE = re.compile(r"\*\*|`")


def plain(text: str) -> str:
    """A bot message without Discord markup, for the panel ('**name** läuft schon.' → 'name läuft schon.')."""
    return _MARKUP_RE.sub("", text)


def backup_files(folder: Path) -> list[dict[str, Any]]:
    """The world backups of one server, newest first: file name, size in bytes, time (ISO, UTC)."""
    if not folder.is_dir():
        return []
    files = []
    for file in folder.iterdir():
        if not file.is_file() or file.name.endswith(".part"):
            continue
        info = file.stat()
        files.append({"file": file.name, "size": info.st_size,
                      "time": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(files, key=lambda f: f["time"], reverse=True)


def status_data(info: dict[str, Any], whitelist: list[str], backups: dict[str, list[dict[str, Any]]],
                last_started: str | None, settings: Settings) -> dict[str, Any]:
    """Everything the Minecraft page shows: which server runs, who is online, all servers with their backups."""
    running = info.get("running")
    players = info.get("players")
    servers = []
    for config, state in info.get("servers") or []:
        servers.append({
            "name": config.name,
            "label": config.label,
            "type": config.type,
            "version": config.version,
            "state": state,
            "created_by": config.created_by,
            "created_at": config.created_at,
            "last": config.name == last_started,
            "backups": backups.get(config.name, []),
        })
    return {
        "running": running,
        "health": info.get("health") if running else None,
        "ready": bool(running) and info.get("health") == "healthy",
        "players": {"online": players[0], "max": players[1], "names": players[2]} if players else None,
        "memory_mb": info.get("memory_mb") if running else None,
        "mem_limit": settings.mem_limit,
        "servers": servers,
        "whitelist": whitelist,
        "address": settings.public_address,
        "lan_address": settings.lan_address,
        "autostop_minutes": settings.autostop_minutes,
        "backup_keep": settings.backup_keep,
    }


def log_lines(raw: str | None, default: int = 200) -> int:
    """How many log lines the panel asked for, within 1 to MAX_LOG_LINES."""
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(1, min(MAX_LOG_LINES, value))
