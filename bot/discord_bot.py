"""Discord side: the /mc slash commands, messages in the channel and the bot's status line."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Settings
from .core import McError
from .servers import ServerManager

log = logging.getLogger("mcbot.discord")

READY_TIMEOUT = 8 * 60  # the first start downloads Paper and generates a world, which takes a while


def _icon(state: str) -> str:
    return {"running": "🟢", "restarting": "🟡"}.get(state, "⚫")


class GuardedTree(app_commands.CommandTree):
    """Answers only in the configured channel and turns errors into short messages."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel_id = interaction.client.settings.channel_id  # type: ignore[attr-defined]
        if interaction.channel_id == channel_id:
            return True
        if interaction.type is not discord.InteractionType.autocomplete:
            await interaction.response.send_message(f"Bitte im <#{channel_id}> benutzen.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure):
            message = "🔒 Das dürfen nur MC-Admins."
        elif isinstance(original, McError):
            message = f"⚠️ {original}"
        else:
            log.error("Command failed", exc_info=original)
            message = "⚠️ Da ist etwas schiefgelaufen. Die Details stehen im Bot-Log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not send the error message")


class McBot(discord.Client):
    def __init__(self, settings: Settings, manager: ServerManager) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.mgr = manager
        self.tree = GuardedTree(self)
        self._ready_watch: dict[str, asyncio.Task[None]] = {}
        self._announced: set[str] = set()

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.add_command(build_commands(self), guild=guild)
        await self.tree.sync(guild=guild)
        loop = asyncio.get_running_loop()

        def forward(name: str, action: str, attrs: dict) -> None:
            asyncio.run_coroutine_threadsafe(self.on_docker_event(name, action, attrs), loop)

        threading.Thread(target=self.mgr.watch_events, args=(forward,), name="docker-events", daemon=True).start()
        for kind in ("paper", "vanilla"):  # load the version lists now, so autocomplete answers quickly
            loop.run_in_executor(None, self.mgr.versions.releases, kind)
        self.presence_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    # --- helpers -----------------------------------------------------------
    async def post(self, text: str) -> None:
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        await channel.send(text)  # type: ignore[union-attr]

    def address_text(self) -> str:
        parts = []
        if self.settings.public_address:
            parts.append(f"Adresse: `{self.settings.public_address}`")
        if self.settings.lan_address:
            parts.append(f"zu Hause: `{self.settings.lan_address}`")
        return " · ".join(parts)

    def watch_ready(self, name: str) -> None:
        self._announced.discard(name)
        old = self._ready_watch.pop(name, None)
        if old:
            old.cancel()
        self._ready_watch[name] = asyncio.create_task(self._ready_timeout(name))

    async def _ready_timeout(self, name: str) -> None:
        await asyncio.sleep(READY_TIMEOUT)
        self._ready_watch.pop(name, None)
        health = await asyncio.to_thread(self.mgr.health, name)
        if health not in (None, "healthy"):
            await self.post(f"⚠️ **{name}** braucht ungewöhnlich lange zum Starten. Ein MC-Admin kann mit `/mc logs` nachschauen.")

    # --- Docker events -----------------------------------------------------
    async def on_docker_event(self, name: str, action: str, attrs: dict[str, Any]) -> None:
        try:
            watch = self._ready_watch.pop(name, None)
            if watch:
                watch.cancel()
            if action == "healthy" and name not in self._announced:
                self._announced.add(name)
                config = await asyncio.to_thread(self.mgr.pin_version, name)
                label = f" ({config.label})" if config else ""
                await self.post(f"✅ **{name}**{label} ist bereit, ihr könnt joinen! {self.address_text()}")
            elif action == "die":
                self._announced.discard(name)
                expected = name in self.mgr.expected_stops
                self.mgr.expected_stops.discard(name)
                code = str(attrs.get("exitCode", ""))
                if not expected and code == "0":
                    await self.post(
                        f"💤 **{name}** hat sich selbst gestoppt, weil {self.settings.autostop_minutes} Minuten "
                        "niemand online war."
                    )
                elif not expected:
                    reason = await asyncio.to_thread(self.mgr.explain_exit, name)
                    await self.post(
                        f"⚠️ {reason}" if reason else
                        f"⚠️ **{name}** ist unerwartet beendet worden (Exit-Code {code}). "
                        f"Ein MC-Admin kann mit `/mc logs {name}` nachschauen."
                    )
                saved = await asyncio.to_thread(self.mgr.backup_after_stop, name)
                if saved:
                    await self.post(f"💾 Backup von **{name}** gespeichert.")
            await self.refresh_presence()
        except Exception:
            log.exception("Handling Docker event %s for %s failed", action, name)

    # --- status line -------------------------------------------------------
    @tasks.loop(seconds=60)
    async def presence_loop(self) -> None:
        await self.refresh_presence()

    @presence_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    async def refresh_presence(self) -> None:
        if not self.is_ready():
            return
        try:
            status = await asyncio.to_thread(self.mgr.quick_status)
        except Exception:
            log.debug("Status not readable", exc_info=True)
            return
        if status is None:
            await self.change_presence(status=discord.Status.idle, activity=discord.Game("kein Server an · /mc start"))
            return
        name, health, players = status
        if health != "healthy":
            text = f"{name} startet…"
        elif players is None:
            text = name
        else:
            text = f"{name} · {players[0]}/{players[1]} online"
        await self.change_presence(status=discord.Status.online, activity=discord.Game(text))


class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Ja, löschen", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="Wird gelöscht…", view=None)
        self.stop()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Abgebrochen, nichts gelöscht.", view=None)
        self.stop()


def status_embed(bot: McBot, info: dict[str, Any]) -> discord.Embed:
    running = info["running"]
    if running is None:
        embed = discord.Embed(
            title="Minecraft",
            description="Gerade läuft kein Server. Starte einen mit `/mc start`.",
            color=discord.Color.dark_grey(),
        )
    else:
        ready = info.get("health") == "healthy"
        embed = discord.Embed(
            title=f"Minecraft · {running}",
            color=discord.Color.green() if ready else discord.Color.orange(),
        )
        embed.add_field(name="Zustand", value="bereit" if ready else "startet…")
        config = next((c for c, _ in info.get("servers") or [] if c.name == running), None)
        if config is not None:
            embed.add_field(name="Version", value=config.label)
        players = info.get("players")
        if players:
            online, maximum, names = players
            embed.add_field(name="Spieler", value=f"{online}/{maximum}" + (f": {', '.join(names)}" if names else ""))
        memory = info.get("memory_mb")
        if memory is not None:
            embed.add_field(name="RAM", value=f"{memory / 1024:.1f} GB")
        if bot.address_text():
            embed.add_field(name="Verbinden", value=bot.address_text(), inline=False)
    servers = info.get("servers") or []
    if servers:
        lines = [f"{_icon(state)} {config.name} · {config.label}" for config, state in servers]
        embed.add_field(name="Alle Server", value="\n".join(lines), inline=False)
    return embed


def build_commands(bot: McBot) -> app_commands.Group:
    mc = app_commands.Group(name="mc", description="Minecraft-Server steuern")
    whitelist = app_commands.Group(name="whitelist", description="Wer mitspielen darf", parent=mc)

    def is_admin(interaction: discord.Interaction) -> bool:
        settings = bot.settings
        if interaction.user.id in settings.admin_user_ids:
            return True
        role_ids = {role.id for role in getattr(interaction.user, "roles", [])}
        return settings.admin_role_id is not None and settings.admin_role_id in role_ids

    admin_only = app_commands.check(is_admin)

    async def server_names(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        configs = await asyncio.to_thread(bot.mgr.store.list)
        needle = current.lower()
        return [app_commands.Choice(name=c.name, value=c.name) for c in configs if needle in c.name][:25]

    # --- for everyone ------------------------------------------------------
    @mc.command(name="start", description="Startet einen Minecraft-Server")
    @app_commands.describe(name="Welcher Server (leer = zuletzt gespielter)")
    @app_commands.autocomplete(name=server_names)
    async def start(interaction: discord.Interaction, name: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        target = await asyncio.to_thread(bot.mgr.resolve, name)
        await asyncio.to_thread(bot.mgr.start, target)
        bot.watch_ready(target)
        config = await asyncio.to_thread(bot.mgr.store.get, target)
        await interaction.followup.send(
            f"⏳ **{target}** ({config.label}) startet, das dauert 1–3 Minuten. "
            "Ich schreibe hier, sobald ihr joinen könnt."
        )
        await bot.refresh_presence()

    @mc.command(name="stop", description="Stoppt den laufenden Server, die Welt wird gespeichert")
    async def stop(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        name = await asyncio.to_thread(bot.mgr.stop, is_admin(interaction))
        await interaction.followup.send(f"⏹️ **{name}** ist gestoppt und die Welt gespeichert.")
        await bot.refresh_presence()

    @mc.command(name="status", description="Zeigt, ob ein Server läuft und wer online ist")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        info = await asyncio.to_thread(bot.mgr.full_status)
        await interaction.followup.send(embed=status_embed(bot, info))

    @mc.command(name="list", description="Zeigt alle Server")
    async def list_servers(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        rows = await asyncio.to_thread(bot.mgr.overview)
        if not rows:
            await interaction.followup.send("Noch keine Server. Ein MC-Admin legt mit `/mc create` einen an.")
            return
        await interaction.followup.send("\n".join(f"{_icon(state)} **{c.name}** · {c.label}" for c, state in rows))

    @whitelist.command(name="show", description="Zeigt, wer mitspielen darf")
    async def whitelist_show(interaction: discord.Interaction) -> None:
        names = await asyncio.to_thread(bot.mgr.store.whitelist)
        text = ", ".join(names) if names else "Noch niemand. Ein MC-Admin trägt Leute mit `/mc whitelist add` ein."
        await interaction.response.send_message(f"📋 Whitelist: {text}")

    # --- MC admins only ----------------------------------------------------
    async def version_names(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        chosen = getattr(interaction.namespace, "typ", None)
        server_type = str(getattr(chosen, "value", chosen) or "paper")
        options = await asyncio.to_thread(bot.mgr.versions.choices, server_type, current)
        return [app_commands.Choice(name=label, value=value) for label, value in options]

    @mc.command(name="create", description="Neuen Server anlegen (nur MC-Admins)")
    @app_commands.describe(
        name="Kurzer Name, z.B. survival",
        typ="Paper (empfohlen, schneller) oder Vanilla",
        version="Tippen zeigt die Versionen, z.B. 26.3 oder 1.21.4",
    )
    @app_commands.choices(
        typ=[
            app_commands.Choice(name="Paper (empfohlen)", value="paper"),
            app_commands.Choice(name="Vanilla", value="vanilla"),
        ]
    )
    @app_commands.autocomplete(version=version_names)
    @admin_only
    async def create(
        interaction: discord.Interaction,
        name: str,
        typ: app_commands.Choice[str],
        version: str,
    ) -> None:
        await interaction.response.defer(thinking=True)
        config = await asyncio.to_thread(bot.mgr.create, name, typ.value, version, str(interaction.user))
        await interaction.followup.send(
            f"🆕 **{config.name}** ({config.label}) ist angelegt. Starten mit `/mc start {config.name}`.\n"
            "Die Version bleibt fest, damit die Welt nicht bei einem Start ungefragt auf eine neue Version wechselt."
        )

    @mc.command(name="delete", description="Server löschen (nur MC-Admins)")
    @app_commands.autocomplete(name=server_names)
    @admin_only
    async def delete(interaction: discord.Interaction, name: str) -> None:
        name = name.strip().lower()
        await asyncio.to_thread(bot.mgr.store.get, name)
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Wirklich **{name}** löschen? Die Welt kommt in den Papierkorb-Ordner auf dem Server "
            "und lässt sich von dort noch retten.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return
        await asyncio.to_thread(bot.mgr.delete, name)
        await interaction.followup.send(f"🗑️ **{name}** wurde gelöscht.")

    @whitelist.command(name="add", description="Spieler darf mitspielen (nur MC-Admins)")
    @app_commands.describe(player="Minecraft-Name")
    @admin_only
    async def whitelist_add(interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(thinking=True)
        name, live, verified = await asyncio.to_thread(bot.mgr.whitelist_add, player)
        extra = f" Gilt sofort auch auf **{live}**." if live else ""
        if not verified:
            extra += (
                "\n⚠️ Ich konnte gerade nicht prüfen, ob es den Namen gibt (playerdb.co und Mojang nicht "
                "erreichbar). Ist er falsch geschrieben, startet kein Server, bis er mit "
                f"`/mc whitelist remove {name}` wieder entfernt ist."
            )
        await interaction.followup.send(f"✅ **{name}** darf jetzt mitspielen.{extra}")

    @whitelist.command(name="remove", description="Spieler von der Whitelist nehmen (nur MC-Admins)")
    @app_commands.describe(player="Minecraft-Name")
    @admin_only
    async def whitelist_remove(interaction: discord.Interaction, player: str) -> None:
        await interaction.response.defer(thinking=True)
        name, _ = await asyncio.to_thread(bot.mgr.whitelist_remove, player)
        await interaction.followup.send(f"🚫 **{name}** ist nicht mehr auf der Whitelist.")

    @mc.command(name="backup", description="Backup der Welt jetzt erstellen (nur MC-Admins)")
    @app_commands.autocomplete(name=server_names)
    @admin_only
    async def backup(interaction: discord.Interaction, name: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        target = await asyncio.to_thread(bot.mgr.resolve, name)
        path = await asyncio.to_thread(bot.mgr.backup, target)
        if path is None:
            await interaction.followup.send(f"**{target}** hat noch keine Welt, nichts zu sichern.")
        else:
            await interaction.followup.send(f"💾 Backup von **{target}** gespeichert ({path.stat().st_size / 1e6:.0f} MB).")

    @mc.command(name="logs", description="Letzte Zeilen aus dem Server-Log (nur MC-Admins)")
    @app_commands.autocomplete(name=server_names)
    @admin_only
    async def logs(interaction: discord.Interaction, name: str | None = None) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        target = await asyncio.to_thread(bot.mgr.resolve, name)
        text = await asyncio.to_thread(bot.mgr.logs, target)
        await interaction.followup.send(f"```\n{text[-1900:] or '(leer)'}\n```", ephemeral=True)

    @mc.command(name="console", description="Befehl in der Server-Konsole ausführen (nur MC-Admins)")
    @app_commands.describe(command="z.B. op Steve oder time set day")
    @admin_only
    async def console(interaction: discord.Interaction, command: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        running = await asyncio.to_thread(bot.mgr.running_name)
        if running is None:
            raise McError("Es läuft gerade kein Server.")
        output = await asyncio.to_thread(bot.mgr.rcon, running, command.strip().lstrip("/"))
        await interaction.followup.send(f"```\n{output[-1900:] or '(keine Ausgabe)'}\n```", ephemeral=True)

    return mc
