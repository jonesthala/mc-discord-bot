"""Optional HTTP API for an admin panel: the same actions as the /mc commands, as JSON.

Switched on with WEB_PORT and WEB_TOKEN in the .env. Every call needs "Authorization: Bearer <WEB_TOKEN>".
Meant for the home network only: whoever has the token can start servers and use the console.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .core import McError, normalize_name
from .webdata import backup_files, log_lines, plain, status_data

if TYPE_CHECKING:
    from .discord_bot import McBot

log = logging.getLogger("mcbot.web")

MAX_BODY = 16 * 1024
PANEL = "Admin-Panel"


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise McError("Die Anfrage ist kein gültiges JSON.") from None
    if not isinstance(data, dict):
        raise McError("Die Anfrage ist kein JSON-Objekt.")
    return data


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def make_app(bot: "McBot") -> web.Application:
    expected = f"Bearer {bot.settings.web_token}".encode()
    mgr = bot.mgr

    @web.middleware
    async def guard(request: web.Request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            return web.json_response({"error": "Kein Zugriff."}, status=401)
        try:
            return await handler(request)
        except McError as error:
            return web.json_response({"error": plain(str(error))}, status=400)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - the panel gets a short message, the details go to the log
            log.exception("API call %s %s failed", request.method, request.path)
            return web.json_response({"error": "Da ist etwas schiefgelaufen. Die Details stehen im Bot-Log."}, status=500)

    async def announce(text: str) -> None:
        """Tells the Minecraft channel, so players know; the panel action counts even if Discord is down."""
        try:
            await bot.post(text)
        except Exception:  # noqa: BLE001
            log.warning("Could not post to Discord: %s", text)

    def snapshot() -> dict[str, Any]:
        info = mgr.full_status()
        backups = {config.name: backup_files(mgr.store.backups / config.name) for config, _ in info["servers"]}
        return status_data(info, mgr.store.whitelist(), backups, mgr.store.last_started(), bot.settings)

    async def status(request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(snapshot))

    async def start(request: web.Request) -> web.Response:
        data = await _body(request)
        target = await asyncio.to_thread(mgr.resolve, _text(data, "name") or None)
        await asyncio.to_thread(mgr.start, target)
        bot.watch_ready(target)
        config = await asyncio.to_thread(mgr.store.get, target)
        await announce(f"⏳ **{target}** ({config.label}) wird gestartet (über das {PANEL}). "
                       "Ich schreibe hier, sobald ihr joinen könnt.")
        await bot.refresh_presence()
        return web.json_response({"message": f"{target} startet, das dauert 1–3 Minuten."})

    async def stop(request: web.Request) -> web.Response:
        data = await _body(request)
        name = await asyncio.to_thread(mgr.stop, data.get("force") is True)
        await announce(f"⏹️ **{name}** wurde gestoppt (über das {PANEL}), die Welt ist gespeichert.")
        await bot.refresh_presence()
        return web.json_response({"message": f"{name} ist gestoppt und die Welt gespeichert."})

    async def backup(request: web.Request) -> web.Response:
        data = await _body(request)
        target = await asyncio.to_thread(mgr.resolve, _text(data, "name") or None)
        path = await asyncio.to_thread(mgr.backup, target)
        if path is None:
            return web.json_response({"message": f"{target} hat noch keine Welt, nichts zu sichern."})
        size = path.stat().st_size
        return web.json_response({"message": f"Backup von {target} gespeichert ({size / 1e6:.0f} MB).", "file": path.name})

    async def logs(request: web.Request) -> web.Response:
        target = await asyncio.to_thread(mgr.resolve, request.query.get("name") or None)
        text = await asyncio.to_thread(mgr.logs, target, log_lines(request.query.get("lines")))
        return web.json_response({"name": target, "text": text})

    async def console(request: web.Request) -> web.Response:
        command = _text(await _body(request), "command").lstrip("/")
        if not command:
            raise McError("Welcher Befehl? Zum Beispiel: time set day")
        running = await asyncio.to_thread(mgr.running_name)
        if running is None:
            raise McError("Es läuft gerade kein Server.")
        output = await asyncio.to_thread(mgr.rcon, running, command)
        log.info("Console on %s from the panel: %s", running, command)
        return web.json_response({"name": running, "output": output})

    async def whitelist_add(request: web.Request) -> web.Response:
        name, live, verified = await asyncio.to_thread(mgr.whitelist_add, _text(await _body(request), "player"))
        message = f"{name} darf jetzt mitspielen." + (f" Gilt sofort auch auf {live}." if live else "")
        if not verified:
            message += (" Achtung: Ob es den Namen gibt, liess sich gerade nicht prüfen. Ist er falsch geschrieben, "
                        "startet kein Server, bis er wieder entfernt ist.")
        return web.json_response({"message": message, "player": name})

    async def whitelist_remove(request: web.Request) -> web.Response:
        name, _ = await asyncio.to_thread(mgr.whitelist_remove, _text(await _body(request), "player"))
        return web.json_response({"message": f"{name} ist nicht mehr auf der Whitelist.", "player": name})

    async def versions(request: web.Request) -> web.Response:
        kind = request.query.get("type", "paper")
        if kind not in ("paper", "vanilla"):
            raise McError("Unbekannter Servertyp.")
        options = await asyncio.to_thread(mgr.versions.choices, kind, request.query.get("q", ""))
        return web.json_response({"versions": [{"label": label, "value": value} for label, value in options]})

    async def create(request: web.Request) -> web.Response:
        data = await _body(request)
        config = await asyncio.to_thread(mgr.create, _text(data, "name"), _text(data, "type") or "paper",
                                         _text(data, "version") or None, PANEL)
        return web.json_response({"message": f"{config.name} ({config.label}) ist angelegt.", "name": config.name})

    async def delete(request: web.Request) -> web.Response:
        name = normalize_name(_text(await _body(request), "name"))
        await asyncio.to_thread(mgr.delete, name)
        return web.json_response({"message": f"{name} ist gelöscht. Die Welt liegt im Papierkorb-Ordner auf dem Server."})

    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app.add_routes([
        web.get("/api/status", status),
        web.post("/api/start", start),
        web.post("/api/stop", stop),
        web.post("/api/backup", backup),
        web.get("/api/logs", logs),
        web.post("/api/console", console),
        web.post("/api/whitelist/add", whitelist_add),
        web.post("/api/whitelist/remove", whitelist_remove),
        web.get("/api/versions", versions),
        web.post("/api/servers/create", create),
        web.post("/api/servers/delete", delete),
    ])
    return app


async def start_web(bot: "McBot") -> web.AppRunner:
    runner = web.AppRunner(make_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", bot.settings.web_port).start()
    log.info("Admin API listening on port %s", bot.settings.web_port)
    return runner
