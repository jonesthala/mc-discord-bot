"""Logic without Docker or Discord: validation, files on disk and world backups.

Only the standard library is used here, so this part can be tested anywhere.
"""
from __future__ import annotations

import json
import re
import shutil
import tarfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

SERVER_TYPES = {"paper": "PAPER", "vanilla": "VANILLA"}
MAX_SERVERS = 10

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,19}$")
_PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
_VERSION_RE = re.compile(r"^(\d+(\.\d+){1,2}(-[A-Za-z0-9.-]+)?|\d{2}w\d{2}[a-z])$")
_COLOR_RE = re.compile(r"§.")
_LIST_RE = re.compile(r"(\d+)\D+?(\d+)[^:]*?players online:?(.*)", re.S | re.I)
# Top-level folders in /data that the server downloads again on start; not worth backing up.
_NOT_IN_BACKUP = {"libraries", "versions", "cache", ".cache", "logs", "crash-reports", "bundler"}


class McError(Exception):
    """An error whose message can be shown to Discord users as it is."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    type: str
    version: str
    created_by: str
    created_at: str

    @property
    def label(self) -> str:
        """'Paper 26.3'; servers from before pinned versions show 'Paper · neueste Version'."""
        if self.version in ("LATEST", "SNAPSHOT"):
            return f"{self.type.capitalize()} · {'neueste Version' if self.version == 'LATEST' else 'Snapshot'}"
        return f"{self.type.capitalize()} {self.version}"


def normalize_name(raw: str) -> str:
    name = raw.strip().lower()
    if not _NAME_RE.match(name):
        raise McError(
            "Der Name braucht 2–20 Zeichen: nur a–z, 0–9 und Bindestrich, "
            "und er darf nicht mit einem Bindestrich anfangen."
        )
    return name


def normalize_version(raw: str | None) -> str:
    version = (raw or "").strip()
    if not version or version.lower() == "latest":
        return "LATEST"
    if version.lower() == "snapshot":
        return "SNAPSHOT"
    if not _VERSION_RE.match(version):
        raise McError("Unbekannte Version. Beispiele: `latest` oder `1.21.4`.")
    return version


def check_player(raw: str) -> str:
    player = raw.strip()
    if not _PLAYER_RE.match(player):
        raise McError("Minecraft-Namen haben 3–16 Zeichen: Buchstaben, Zahlen und _.")
    return player


def parse_player_list(text: str) -> tuple[int, int, list[str]] | None:
    """Reads the answer of the `list` console command: (online, max, names)."""
    match = _LIST_RE.search(_COLOR_RE.sub("", text))
    if not match:
        return None
    names = [part.strip() for part in re.split(r"[,\n]", match.group(3))]
    return int(match.group(1)), int(match.group(2)), [n for n in names if _PLAYER_RE.match(n)]


_STARTING_RE = re.compile(r"Starting minecraft server version (\S+)")
_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def version_from_log(text: str) -> str | None:
    """The version a server reported while starting ('Starting minecraft server version 26.3')."""
    match = _STARTING_RE.search(_ESCAPE_RE.sub("", text))
    return match.group(1) if match else None


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _backup_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = info.name.split("/")
    if len(parts) >= 2:
        top = parts[1]
        if top in _NOT_IN_BACKUP or (len(parts) == 2 and top.endswith(".jar")):
            return None
    return info


class Store:
    """Everything the bot keeps on disk, below one folder (default /srv/minecraft).

    servers/<name>/mcbot.json   settings of a server
    servers/<name>/data/        the server's /data folder (world, plugins, ...)
    backups/<name>/*.tar.gz     world backups, the newest few are kept
    trash/<name>_<time>/        deleted servers, kept until someone removes them by hand
    whitelist.txt               player names that may join, shared by all servers
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.servers = self.root / "servers"
        self.backups = self.root / "backups"
        self.trash = self.root / "trash"
        self.whitelist_file = self.root / "whitelist.txt"
        self.state_file = self.root / "state.json"
        for folder in (self.servers, self.backups, self.trash):
            folder.mkdir(parents=True, exist_ok=True)

    # --- servers -----------------------------------------------------------
    def server_dir(self, name: str) -> Path:
        return self.servers / name

    def data_dir(self, name: str) -> Path:
        return self.server_dir(name) / "data"

    def list(self) -> list[ServerConfig]:
        configs = []
        for file in sorted(self.servers.glob("*/mcbot.json")):
            try:
                configs.append(ServerConfig(**json.loads(file.read_text(encoding="utf-8"))))
            except (ValueError, TypeError):
                continue
        return configs

    def get(self, name: str) -> ServerConfig:
        file = self.server_dir(name) / "mcbot.json"
        if not file.is_file():
            raise McError(f"Einen Server **{name}** gibt es nicht. Alle Server: `/mc list`")
        return ServerConfig(**json.loads(file.read_text(encoding="utf-8")))

    def create(self, name: str, server_type: str, version: str | None, created_by: str) -> ServerConfig:
        name = normalize_name(name)
        if server_type not in SERVER_TYPES:
            raise McError("Unbekannter Servertyp.")
        version = normalize_version(version)
        if self.server_dir(name).exists():
            raise McError(f"**{name}** gibt es schon.")
        if len(self.list()) >= MAX_SERVERS:
            raise McError(f"Es gibt schon {MAX_SERVERS} Server. Lösche zuerst einen mit `/mc delete`.")
        config = ServerConfig(name, server_type, version, created_by, datetime.now().isoformat(timespec="seconds"))
        self.data_dir(name).mkdir(parents=True)
        (self.server_dir(name) / "mcbot.json").write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
        return config

    def set_version(self, name: str, version: str) -> ServerConfig:
        """Pins a server to a fixed version (used for servers created as 'latest')."""
        config = replace(self.get(name), version=normalize_version(version))
        (self.server_dir(name) / "mcbot.json").write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
        return config

    def move_to_trash(self, name: str) -> Path:
        self.get(name)
        target = self.trash / f"{name}_{_stamp()}"
        shutil.move(str(self.server_dir(name)), str(target))
        if self.last_started() == name:
            self.set_last_started(None)
        return target

    # --- whitelist ---------------------------------------------------------
    def whitelist(self) -> list[str]:
        if not self.whitelist_file.is_file():
            return []
        lines = self.whitelist_file.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]

    def whitelist_add(self, player: str) -> str:
        player = check_player(player)
        names = self.whitelist()
        if any(name.lower() == player.lower() for name in names):
            raise McError(f"**{player}** steht schon auf der Whitelist.")
        self._write_whitelist([*names, player])
        return player

    def whitelist_remove(self, player: str) -> str:
        names = self.whitelist()
        match = next((name for name in names if name.lower() == player.strip().lower()), None)
        if match is None:
            raise McError(f"**{player.strip()}** steht nicht auf der Whitelist.")
        self._write_whitelist([name for name in names if name != match])
        return match

    def prune_whitelist_json(self, name: str, player: str) -> None:
        """Removes a player from a stopped server's own whitelist.json."""
        file = self.data_dir(name) / "whitelist.json"
        if not file.is_file():
            return
        try:
            entries = json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            return
        kept = [entry for entry in entries if str(entry.get("name", "")).lower() != player.lower()]
        if len(kept) != len(entries):
            file.write_text(json.dumps(kept, indent=2), encoding="utf-8")

    def _write_whitelist(self, names: list[str]) -> None:
        self.whitelist_file.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")

    # --- remembered state --------------------------------------------------
    def last_started(self) -> str | None:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8")).get("last_started")
        except (OSError, ValueError):
            return None

    def set_last_started(self, name: str | None) -> None:
        self.state_file.write_text(json.dumps({"last_started": name}), encoding="utf-8")

    # --- backups -----------------------------------------------------------
    def backup(self, name: str, keep: int) -> Path | None:
        """Packs the server folder into backups/<name>/. None if no world exists yet."""
        data = self.data_dir(name)
        if not (data / "world").is_dir():
            return None
        folder = self.backups / name
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{name}_{_stamp()}.tar.gz"
        partial = target.with_name(target.name + ".part")
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            tar.add(data, arcname=name, filter=_backup_filter)
        partial.replace(target)
        self._prune_backups(folder, keep)
        return target

    def backups_of(self, name: str) -> list[Path]:
        return sorted((self.backups / name).glob(f"{name}_*.tar.gz"))

    @staticmethod
    def _prune_backups(folder: Path, keep: int) -> None:
        if keep <= 0:
            return
        files = sorted(folder.glob("*.tar.gz"))
        for old in files[:-keep]:
            old.unlink()
