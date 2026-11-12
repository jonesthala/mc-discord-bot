"""Settings, read from environment variables (the .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str) -> str:
    value = _text(name)
    if not value:
        raise ConfigError(f"Environment variable {name} is missing")
    return value


def _int(name: str, default: int | None = None) -> int:
    raw = _text(name)
    if not raw:
        if default is None:
            raise ConfigError(f"Environment variable {name} is missing")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _id_set(name: str) -> frozenset[int]:
    raw = _text(name).replace(" ", "")
    try:
        return frozenset(int(part) for part in raw.split(",") if part)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a comma-separated list of numbers") from exc


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int
    channel_id: int
    admin_role_id: int | None
    admin_user_ids: frozenset[int]
    eula_accepted: bool
    data_dir: Path
    image: str
    memory: str
    mem_limit: str
    autostop_minutes: int
    max_players: int
    public_address: str
    lan_address: str
    backup_keep: int
    timezone: str
    motd: str = "Minecraft · {name}"

    @classmethod
    def from_env(cls) -> "Settings":
        role_id = _int("DISCORD_ADMIN_ROLE_ID", 0)
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            guild_id=_int("DISCORD_GUILD_ID"),
            channel_id=_int("DISCORD_CHANNEL_ID"),
            admin_role_id=role_id or None,
            admin_user_ids=_id_set("DISCORD_ADMIN_USER_IDS"),
            eula_accepted=_text("MC_EULA").upper() == "TRUE",
            data_dir=Path(_text("MC_DATA_DIR", "/srv/minecraft")),
            image=_text("MC_IMAGE", "itzg/minecraft-server:latest"),
            memory=_text("MC_MEMORY", "2560M"),
            mem_limit=_text("MC_MEM_LIMIT", "3200m"),
            autostop_minutes=_int("MC_AUTOSTOP_MINUTES", 15),
            max_players=_int("MC_MAX_PLAYERS", 10),
            public_address=_text("MC_PUBLIC_ADDRESS"),
            lan_address=_text("MC_LAN_ADDRESS"),
            backup_keep=_int("MC_BACKUP_KEEP", 5),
            timezone=_text("TZ", "UTC"),
            motd=_text("MC_MOTD", "Minecraft · {name}"),
        )

    def motd_for(self, server: str) -> str:
        """Server list text; {name} is replaced by the server name."""
        return self.motd.replace("{name}", server)[:59]
