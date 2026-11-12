import logging
import sys

import discord

from .config import ConfigError, Settings
from .discord_bot import McBot
from .servers import ServerManager


def main() -> int:
    discord.utils.setup_logging(level=logging.INFO)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.getLogger("mcbot").error("%s", exc)
        return 2
    McBot(settings, ServerManager(settings)).run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
