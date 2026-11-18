import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from bot.core import (
    McError,
    Store,
    check_player,
    explain_exit,
    normalize_name,
    normalize_version,
    parse_player_list,
    version_from_log,
)


class ValidationTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(normalize_name("  Survival "), "survival")
        self.assertEqual(normalize_name("sky-block2"), "sky-block2")
        for bad in ("a", "-start", "with space", "x" * 21, "ümlaut", "../etc"):
            with self.assertRaises(McError, msg=bad):
                normalize_name(bad)

    def test_versions(self):
        self.assertEqual(normalize_version(None), "LATEST")
        self.assertEqual(normalize_version("latest"), "LATEST")
        self.assertEqual(normalize_version("Snapshot"), "SNAPSHOT")
        self.assertEqual(normalize_version("1.21.4"), "1.21.4")
        self.assertEqual(normalize_version("26.1"), "26.1")
        self.assertEqual(normalize_version("1.21.5-pre1"), "1.21.5-pre1")
        self.assertEqual(normalize_version("25w14a"), "25w14a")
        for bad in ("1", "abc", "1.21; rm -rf /"):
            with self.assertRaises(McError, msg=bad):
                normalize_version(bad)

    def test_players(self):
        self.assertEqual(check_player(" Steve_01 "), "Steve_01")
        for bad in ("ab", "x" * 17, "no space", "semi;colon"):
            with self.assertRaises(McError, msg=bad):
                check_player(bad)


class PlayerListTest(unittest.TestCase):
    def test_vanilla(self):
        self.assertEqual(
            parse_player_list("There are 2 of a max of 10 players online: Steve, Alex"),
            (2, 10, ["Steve", "Alex"]),
        )

    def test_empty(self):
        self.assertEqual(parse_player_list("There are 0 of a max of 20 players online:"), (0, 20, []))

    def test_colored_other_wording(self):
        self.assertEqual(
            parse_player_list("§6There are §c1§6 out of maximum §c20§6 players online."),
            (1, 20, []),
        )

    def test_garbage(self):
        self.assertIsNone(parse_player_list("Unknown command"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_list_get(self):
        config = self.store.create("Survival", "paper", None, "tester")
        self.assertEqual(config.name, "survival")
        self.assertEqual(config.version, "LATEST")
        self.assertEqual([c.name for c in self.store.list()], ["survival"])
        self.assertEqual(self.store.get("survival").type, "paper")
        self.assertTrue(self.store.data_dir("survival").is_dir())

    def test_create_rejects_duplicates_and_unknown_types(self):
        self.store.create("survival", "paper", None, "tester")
        with self.assertRaises(McError):
            self.store.create("survival", "paper", None, "tester")
        with self.assertRaises(McError):
            self.store.create("modded", "forge", None, "tester")

    def test_get_unknown(self):
        with self.assertRaises(McError):
            self.store.get("nope")

    def test_whitelist(self):
        self.store.whitelist_add("Steve")
        self.store.whitelist_add("Alex")
        with self.assertRaises(McError):
            self.store.whitelist_add("steve")
        self.assertEqual(self.store.whitelist_remove("STEVE"), "Steve")
        self.assertEqual(self.store.whitelist(), ["Alex"])
        with self.assertRaises(McError):
            self.store.whitelist_remove("Steve")

    def test_prune_whitelist_json(self):
        self.store.create("survival", "paper", None, "tester")
        file = self.store.data_dir("survival") / "whitelist.json"
        file.write_text(json.dumps([{"uuid": "1", "name": "Steve"}, {"uuid": "2", "name": "Alex"}]))
        self.store.prune_whitelist_json("survival", "steve")
        self.assertEqual([e["name"] for e in json.loads(file.read_text())], ["Alex"])

    def test_last_started(self):
        self.assertIsNone(self.store.last_started())
        self.store.set_last_started("survival")
        self.assertEqual(self.store.last_started(), "survival")

    def test_backup_skips_downloads_and_keeps_newest(self):
        self.store.create("survival", "paper", None, "tester")
        data = self.store.data_dir("survival")
        self.assertIsNone(self.store.backup("survival", keep=2))  # no world yet
        (data / "world").mkdir()
        (data / "world" / "level.dat").write_bytes(b"level")
        (data / "plugins").mkdir()
        (data / "plugins" / "essentials.jar").write_bytes(b"plugin")
        (data / "libraries").mkdir()
        (data / "libraries" / "big.jar").write_bytes(b"x" * 1000)
        (data / "paper.jar").write_bytes(b"server")
        (data / "server.properties").write_text("motd=test")

        path = self.store.backup("survival", keep=2)
        with tarfile.open(path) as tar:
            names = set(tar.getnames())
        self.assertIn("survival/world/level.dat", names)
        self.assertIn("survival/plugins/essentials.jar", names)
        self.assertIn("survival/server.properties", names)
        self.assertNotIn("survival/paper.jar", names)
        self.assertFalse(any(n.startswith("survival/libraries") for n in names))

        for index in range(3):  # names carry a timestamp; fake older ones
            (self.store.backups / "survival" / f"survival_2000-01-0{index + 1}_00-00-00.tar.gz").write_bytes(b"")
        self.store._prune_backups(self.store.backups / "survival", keep=2)
        remaining = [p.name for p in self.store.backups_of("survival")]
        self.assertEqual(len(remaining), 2)
        self.assertIn(path.name, remaining)

    def test_move_to_trash(self):
        self.store.create("survival", "paper", None, "tester")
        self.store.set_last_started("survival")
        target = self.store.move_to_trash("survival")
        self.assertTrue((target / "mcbot.json").is_file())
        self.assertEqual(self.store.list(), [])
        self.assertIsNone(self.store.last_started())

    def test_label_and_pinning(self):
        legacy = self.store.create("legacy", "paper", None, "admin")
        self.assertEqual(legacy.label, "Paper · neueste Version")
        pinned = self.store.set_version("legacy", "26.3")
        self.assertEqual(pinned.version, "26.3")
        self.assertEqual(pinned.label, "Paper 26.3")
        self.assertEqual(self.store.get("legacy").version, "26.3")
        self.assertEqual(self.store.get("legacy").created_by, "admin")
        self.assertEqual(self.store.create("old", "vanilla", "1.21.4", "admin").label, "Vanilla 1.21.4")
        with self.assertRaises(McError):
            self.store.set_version("legacy", "1.21; rm")


class VersionFromLogTest(unittest.TestCase):
    def test_reads_version(self):
        log = (
            "[init] Resolved version\n"
            "\x1b[37m[00:17:34 INFO]: Starting minecraft server version 26.3\x1b[0m\n"
            "[00:17:34 INFO]: This server is running Paper version 26.3-249-main\n"
        )
        self.assertEqual(version_from_log(log), "26.3")
        self.assertIsNone(version_from_log("nothing here"))


class ExplainExitTest(unittest.TestCase):
    def test_unknown_whitelist_name(self):
        log = (
            "[init] Running as uid=1000 gid=1000 with /data as 'drwxr-xr-x 1000 1000 /data'\n"
            "[init] Resolving type given VANILLA\n"
            "\x1b[31m[mc-image-helper] 14:02:11.123 ERROR : Invalid parameter provided for 'manage-users' "
            "command: Could not resolve user from Playerdb: Stevee_01\x1b[0m\n"
        )
        message = explain_exit("survival", log)
        self.assertIn("**survival**", message)
        self.assertIn("**Stevee_01**", message)
        self.assertIn("`/mc whitelist remove Stevee_01`", message)
        self.assertNotIn("ß", message)

    def test_other_provider_and_quotes(self):
        message = explain_exit("sky", "ERROR : Could not resolve user from Mojang: 'Alex_2'\n")
        self.assertIn("`/mc whitelist remove Alex_2`", message)

    def test_nothing_known(self):
        self.assertIsNone(explain_exit("survival", ""))
        self.assertIsNone(explain_exit("survival", "java.lang.OutOfMemoryError: Java heap space\n"))


if __name__ == "__main__":
    unittest.main()
