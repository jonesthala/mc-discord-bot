import unittest
from pathlib import Path
from unittest import mock

from bot.config import ConfigError, Settings

BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2"}


class SettingsTest(unittest.TestCase):
    def load(self, **extra):
        with mock.patch.dict("os.environ", {**BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_defaults(self):
        s = self.load()
        self.assertEqual((s.guild_id, s.channel_id, s.admin_role_id), (1, 2, None))
        self.assertFalse(s.eula_accepted)
        self.assertEqual(s.data_dir, Path("/srv/minecraft"))
        self.assertEqual(s.timezone, "UTC")
        self.assertEqual(s.motd_for("survival"), "Minecraft · survival")

    def test_values(self):
        s = self.load(MC_EULA="true", DISCORD_ADMIN_ROLE_ID="7", DISCORD_ADMIN_USER_IDS="3, 4",
                      MC_MOTD="Homelab · {name}", MC_AUTOSTOP_MINUTES="5")
        self.assertTrue(s.eula_accepted)
        self.assertEqual(s.admin_role_id, 7)
        self.assertEqual(s.admin_user_ids, frozenset({3, 4}))
        self.assertEqual(s.motd_for("sky"), "Homelab · sky")
        self.assertEqual(s.autostop_minutes, 5)
        self.assertLessEqual(len(self.load(MC_MOTD="x" * 100).motd_for("a")), 59)

    def test_errors(self):
        for env in ({"DISCORD_TOKEN": ""}, {"DISCORD_GUILD_ID": "abc"}, {"DISCORD_ADMIN_USER_IDS": "1,x"}):
            with self.assertRaises(ConfigError, msg=env):
                self.load(**env)


if __name__ == "__main__":
    unittest.main()
