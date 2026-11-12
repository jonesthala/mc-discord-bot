"""Runs the Minecraft servers as Docker containers (image itzg/minecraft-server).

The bot talks to the local Docker daemon through the Docker socket. Run it on a dedicated
VM or LXC: whoever controls that socket controls the Docker host.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import docker
from docker.errors import ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool

from .config import Settings
from .core import SERVER_TYPES, McError, ServerConfig, Store, normalize_name, parse_player_list, version_from_log
from .versions import VersionCatalog

log = logging.getLogger("mcbot.servers")

LABEL = "mcbot.managed"
LABEL_SERVER = "mcbot.server"
NETWORK = "mcnet"
NETWORK_SUBNET = "172.30.30.0/24"  # must match deploy/mc-isolate.sh
PORT = 25565
PUBLIC_DNS = ["9.9.9.9", "1.1.1.1"]  # so the servers never need DNS from the home network
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

EventCallback = Callable[[str, str, dict], None]


def container_name(server: str) -> str:
    return f"mc-{server}"


class ServerManager:
    def __init__(self, settings: Settings, store: Store | None = None, client: Any = None,
                 versions: VersionCatalog | None = None) -> None:
        self.s = settings
        self.store = store or Store(settings.data_dir)
        self.client = client or docker.from_env()
        self.versions = versions or VersionCatalog()
        self.lock = threading.Lock()
        self.expected_stops: set[str] = set()

    # --- lookups -----------------------------------------------------------
    def _container(self, name: str):
        try:
            return self.client.containers.get(container_name(name))
        except NotFound:
            return None

    def running_name(self) -> str | None:
        found = self.client.containers.list(filters={"label": f"{LABEL}=true", "status": "running"})
        return found[0].labels.get(LABEL_SERVER) if found else None

    def overview(self) -> list[tuple[ServerConfig, str]]:
        containers = self.client.containers.list(all=True, filters={"label": f"{LABEL}=true"})
        states = {c.labels.get(LABEL_SERVER): c.status for c in containers}
        return [(config, states.get(config.name, "stopped")) for config in self.store.list()]

    def health(self, name: str) -> str | None:
        """'starting', 'healthy', 'unhealthy', or None if the server is not running."""
        container = self._container(name)
        if container is None or container.status != "running":
            return None
        return (container.attrs.get("State", {}).get("Health") or {}).get("Status", "unknown")

    def resolve(self, name: str | None) -> str:
        """Which server a command means; without a name the last one started."""
        if name and name.strip():
            server = name.strip().lower()
            self.store.get(server)
            return server
        last = self.store.last_started()
        if last and (self.store.server_dir(last) / "mcbot.json").is_file():
            return last
        servers = self.store.list()
        if len(servers) == 1:
            return servers[0].name
        if not servers:
            raise McError("Es gibt noch keinen Server. Ein MC-Admin legt mit `/mc create` einen an.")
        raise McError("Welcher Server? Zum Beispiel `/mc start survival`. Alle Server: `/mc list`")

    # --- start / stop ------------------------------------------------------
    def start(self, name: str) -> None:
        if not self.s.eula_accepted:
            raise McError("Die Minecraft-EULA ist noch nicht akzeptiert (`MC_EULA=TRUE` fehlt in der .env).")
        config = self.store.get(name)
        with self.lock:
            current = self.running_name()
            if current == name:
                raise McError(f"**{name}** läuft schon.")
            if current:
                raise McError(
                    f"Es läuft schon **{current}**, und es kann nur ein Server gleichzeitig laufen. "
                    "Zuerst `/mc stop`."
                )
            self._ensure_network()
            self._ensure_image()
            old = self._container(name)
            if old is not None:
                old.remove(force=True)  # stopped container of the last run; the world lives in data/
            self.client.containers.run(
                self.s.image,
                name=container_name(name),
                detach=True,
                environment=self._environment(config),
                labels={LABEL: "true", LABEL_SERVER: name},
                ports={f"{PORT}/tcp": PORT},
                # The bot sees /srv/minecraft at the same path as the Docker daemon does.
                volumes={str(self.store.data_dir(name)): {"bind": "/data", "mode": "rw"}},
                network=NETWORK,
                dns=PUBLIC_DNS,
                mem_limit=self.s.mem_limit,
                restart_policy={"Name": "no"},
            )
            self.store.set_last_started(name)

    def stop(self, force: bool = False) -> str:
        with self.lock:
            name = self.running_name()
            if name is None:
                raise McError("Es läuft gerade kein Server.")
            if not force:
                try:
                    players = self.players(name)
                except McError:
                    players = None
                if players and players[0] > 0:
                    who = ", ".join(players[2]) or f"{players[0]} Spieler"
                    raise McError(
                        f"Auf **{name}** sind noch Leute online ({who}). Dann dürfen nur MC-Admins stoppen. "
                        "Sonst stoppt der Server von selbst, wenn alle weg sind."
                    )
            container = self._container(name)
            if container is not None:
                self.expected_stops.add(name)
                container.stop(timeout=90)
            return name

    def _environment(self, config: ServerConfig) -> dict[str, str]:
        autostop = str(self.s.autostop_minutes * 60)
        env = {
            "EULA": "TRUE",
            "TYPE": SERVER_TYPES[config.type],
            "VERSION": config.version,
            "MEMORY": self.s.memory,
            "USE_AIKAR_FLAGS": "true",
            "TZ": self.s.timezone,
            "MOTD": self.s.motd_for(config.name),
            "MAX_PLAYERS": str(self.s.max_players),
            "VIEW_DISTANCE": "8",
            "SIMULATION_DISTANCE": "6",
            "ONLINE_MODE": "TRUE",
            "ENABLE_WHITELIST": "TRUE",
            "ENFORCE_WHITELIST": "TRUE",
            "EXISTING_WHITELIST_FILE": "SYNCHRONIZE",
            "ENABLE_AUTOSTOP": "TRUE",
            "AUTOSTOP_TIMEOUT_EST": autostop,
            "AUTOSTOP_TIMEOUT_INIT": autostop,
        }
        names = self.store.whitelist()
        if names:
            env["WHITELIST"] = ",".join(names)
        return env

    def _ensure_network(self) -> None:
        try:
            self.client.networks.get(NETWORK)
        except NotFound:
            ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=NETWORK_SUBNET)])
            self.client.networks.create(NETWORK, driver="bridge", ipam=ipam, labels={LABEL: "true"})

    def _ensure_image(self) -> None:
        try:
            self.client.images.get(self.s.image)
        except ImageNotFound:
            log.info("Pulling %s", self.s.image)
            self.client.images.pull(self.s.image)

    # --- console -----------------------------------------------------------
    def rcon(self, name: str, command: str) -> str:
        container = self._container(name)
        if container is None or container.status != "running":
            raise McError(f"**{name}** läuft nicht.")
        code, output = container.exec_run(["rcon-cli", command])
        text = _ANSI_RE.sub("", output.decode("utf-8", "replace")).strip()
        if code != 0:
            raise McError("Der Server antwortet noch nicht. Startet er gerade?")
        return text

    def players(self, name: str) -> tuple[int, int, list[str]] | None:
        return parse_player_list(self.rcon(name, "list"))

    def memory_mb(self, name: str) -> int | None:
        container = self._container(name)
        if container is None or container.status != "running":
            return None
        stats = container.stats(stream=False, one_shot=True).get("memory_stats", {})
        used = stats.get("usage", 0) - stats.get("stats", {}).get("inactive_file", 0)
        return max(used, 0) // (1024 * 1024)

    def logs(self, name: str, lines: int = 30) -> str:
        container = self._container(name)
        if container is None:
            raise McError(f"**{name}** wurde noch nie gestartet.")
        return _ANSI_RE.sub("", container.logs(tail=lines).decode("utf-8", "replace"))

    def quick_status(self) -> tuple[str, str | None, tuple[int, int, list[str]] | None] | None:
        name = self.running_name()
        if name is None:
            return None
        health = self.health(name)
        players = None
        if health == "healthy":
            try:
                players = self.players(name)
            except McError:
                pass
        return name, health, players

    def full_status(self) -> dict[str, Any]:
        name = self.running_name()
        info: dict[str, Any] = {"running": name, "servers": self.overview()}
        if name:
            info["health"] = self.health(name)
            try:
                info["players"] = self.players(name)
            except McError:
                info["players"] = None
            info["memory_mb"] = self.memory_mb(name)
        return info

    # --- create / delete ---------------------------------------------------
    def create(self, name: str, server_type: str, version: str | None, created_by: str) -> ServerConfig:
        if server_type not in SERVER_TYPES:
            raise McError("Unbekannter Servertyp.")
        fixed = self.versions.resolve(server_type, version)  # network call, so outside the lock
        with self.lock:
            return self.store.create(name, server_type, fixed, created_by)

    def pin_version(self, name: str) -> ServerConfig | None:
        """Called when a server is ready. Servers created as 'latest' get the version they
        actually started with written into their settings, so a later start cannot upgrade
        the world by surprise. Returns the (possibly updated) settings."""
        try:
            config = self.store.get(name)
        except McError:
            return None
        if config.version not in ("LATEST", "SNAPSHOT"):
            return config
        container = self._container(name)
        if container is None:
            return config
        found = version_from_log(container.logs(tail=5000).decode("utf-8", "replace"))
        if not found:
            return config
        try:
            return self.store.set_version(name, found)
        except McError:
            log.warning("Could not pin %s to version %r", name, found)
            return config

    def delete(self, name: str) -> Path:
        name = normalize_name(name)
        with self.lock:
            self.store.get(name)
            container = self._container(name)
            if container is not None:
                if container.status == "running":
                    raise McError(f"**{name}** läuft noch. Zuerst `/mc stop`.")
                container.remove()
            return self.store.move_to_trash(name)

    # --- whitelist ---------------------------------------------------------
    def whitelist_add(self, player: str) -> tuple[str, str | None]:
        player = self.store.whitelist_add(player)
        running = self.running_name()
        if running:
            try:
                self.rcon(running, f"whitelist add {player}")
            except McError:
                running = None
        return player, running

    def whitelist_remove(self, player: str) -> tuple[str, str | None]:
        player = self.store.whitelist_remove(player)
        running = self.running_name()
        for config in self.store.list():
            if config.name != running:
                self.store.prune_whitelist_json(config.name, player)
        if running:
            try:
                self.rcon(running, f"whitelist remove {player}")
            except McError:
                running = None
        return player, running

    # --- backups -----------------------------------------------------------
    def backup(self, name: str) -> Path | None:
        """Backup on demand. A running server pauses saving while the files are copied."""
        self.store.get(name)
        running = self.running_name() == name
        if running:
            self.rcon(name, "save-off")
            self.rcon(name, "save-all flush")
        try:
            return self.store.backup(name, self.s.backup_keep)
        finally:
            if running:
                try:
                    self.rcon(name, "save-on")
                except McError:
                    log.warning("save-on failed for %s", name)

    def backup_after_stop(self, name: str) -> Path | None:
        if self.running_name() == name:
            return None
        try:
            self.store.get(name)
        except McError:
            return None  # deleted in the meantime
        return self.store.backup(name, self.s.backup_keep)

    # --- events ------------------------------------------------------------
    def watch_events(self, callback: EventCallback) -> None:
        """Blocking loop for a thread: reports 'healthy' and 'die' of the server containers."""
        while True:
            try:
                events = docker.from_env().events(
                    decode=True, filters={"type": "container", "label": f"{LABEL}=true"}
                )
                for event in events:
                    action = str(event.get("Action") or event.get("status") or "")
                    attrs = (event.get("Actor") or {}).get("Attributes") or {}
                    name = attrs.get(LABEL_SERVER)
                    if not name:
                        continue
                    if action.startswith("health_status") and action.split(":", 1)[-1].strip() == "healthy":
                        callback(name, "healthy", attrs)
                    elif action == "die":
                        callback(name, "die", attrs)
            except Exception:
                log.exception("Lost the Docker event stream, reconnecting in 5 s")
            time.sleep(5)
