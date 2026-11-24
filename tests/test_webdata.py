import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot.config import Settings
from bot.core import ServerConfig
from bot.webdata import backup_files, log_lines, plain, status_data

ENV = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "MC_PUBLIC_ADDRESS": "play.example",
       "MC_LAN_ADDRESS": "192.168.1.5", "MC_MEM_LIMIT": "3000m", "MC_BACKUP_KEEP": "2"}


def settings() -> Settings:
    with mock.patch.dict("os.environ", ENV, clear=True):
        return Settings.from_env()


class StatusDataTest(unittest.TestCase):
    def setUp(self):
        self.world = ServerConfig("world", "vanilla", "1.21.5", "Admin", "2026-11-14T10:00:00")
        self.sky = ServerConfig("sky", "paper", "LATEST", "Admin", "2026-11-15T10:00:00")

    def test_running_server(self):
        info = {"running": "world", "health": "healthy", "players": (2, 10, ["Alex", "Steve"]), "memory_mb": 1800,
                "servers": [(self.world, "running"), (self.sky, "exited")]}
        backups = {"world": [{"file": "world_1.tar.gz", "size": 5, "time": "2026-11-15T21:10:04+00:00"}]}
        data = status_data(info, ["Alex", "Steve"], backups, "world", settings())
        self.assertEqual(data["running"], "world")
        self.assertTrue(data["ready"])
        self.assertEqual(data["players"], {"online": 2, "max": 10, "names": ["Alex", "Steve"]})
        self.assertEqual(data["memory_mb"], 1800)
        self.assertEqual([s["name"] for s in data["servers"]], ["world", "sky"])
        self.assertEqual(data["servers"][0]["label"], "Vanilla 1.21.5")
        self.assertTrue(data["servers"][0]["last"])
        self.assertEqual(data["servers"][0]["backups"][0]["file"], "world_1.tar.gz")
        self.assertEqual(data["servers"][1]["backups"], [])
        self.assertEqual(data["servers"][1]["label"], "Paper · neueste Version")
        self.assertEqual((data["address"], data["lan_address"], data["mem_limit"]), ("play.example", "192.168.1.5", "3000m"))

    def test_nothing_running(self):
        info = {"running": None, "servers": [(self.world, "exited")]}
        data = status_data(info, [], {}, None, settings())
        self.assertIsNone(data["running"])
        self.assertFalse(data["ready"])
        self.assertIsNone(data["players"])
        self.assertIsNone(data["memory_mb"])
        self.assertFalse(data["servers"][0]["last"])

    def test_starting_server(self):
        info = {"running": "world", "health": "starting", "players": None, "memory_mb": 900,
                "servers": [(self.world, "running")]}
        data = status_data(info, [], {}, "world", settings())
        self.assertFalse(data["ready"])
        self.assertEqual(data["health"], "starting")


class HelpersTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(plain("**world** läuft schon. Zuerst `/mc stop`."), "world läuft schon. Zuerst /mc stop.")

    def test_log_lines(self):
        self.assertEqual(log_lines(None), 200)
        self.assertEqual(log_lines("50"), 50)
        self.assertEqual(log_lines("0"), 1)
        self.assertEqual(log_lines("99999"), 1000)
        self.assertEqual(log_lines("abc"), 200)

    def test_backup_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(backup_files(folder / "missing"), [])
            for name, when in (("old.tar.gz", 1_700_000_000), ("new.tar.gz", 1_800_000_000), ("x.tar.gz.part", 1_900_000_000)):
                (folder / name).write_bytes(b"12345")
                os.utime(folder / name, (when, when))
            (folder / "sub").mkdir()
            files = backup_files(folder)
            self.assertEqual([f["file"] for f in files], ["new.tar.gz", "old.tar.gz"])
            self.assertEqual(files[0]["size"], 5)
            self.assertTrue(files[0]["time"].endswith("+00:00"))


if __name__ == "__main__":
    unittest.main()
