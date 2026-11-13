"""Delete the slash commands this bot has registered on Discord, without starting the game.

Usage (stop the bot first):
    python purge_commands.py                      # purge GLOBAL commands only
    PURGE_GUILD_ID=123456789 python purge_commands.py   # ...and also that server's commands
(on Windows PowerShell:  $env:PURGE_GUILD_ID="123456789"; python purge_commands.py)

Afterwards start the bot normally; it registers its commands again from scratch.
"""
import os

import discord
from discord import app_commands

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass


class Purger(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)     # deliberately empty

    async def setup_hook(self):
        # Syncing an empty tree replaces Discord's list of commands with an empty one.
        await self.tree.sync()
        print("Global commands purged.")
        guild_id = os.environ.get("PURGE_GUILD_ID")
        if guild_id:
            await self.tree.sync(guild=discord.Object(int(guild_id)))
            print(f"Commands in server {guild_id} purged.")
        await self.close()


token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise SystemExit("Set DISCORD_TOKEN (in .env or the environment).")
Purger().run(token, log_handler=None)