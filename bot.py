import asyncio
import logging
import os
import random
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

import config
import db
import images
from maps import creators_text, is_match, load_maps
from messages import Messages

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("guessmap")

CORRECT_KEYS = ["correct_first", "correct_second", "correct_third"]


@dataclass
class Round:
    guild_id: int
    channel_id: int
    game_map: dict
    path: str
    attempts: dict = field(default_factory=dict)   # user_id -> guesses used
    finished: bool = False
    timeout_task: asyncio.Task | None = None
    message: discord.Message | None = None


class GuessModal(discord.ui.Modal, title="Guess the map"):
    guess = discord.ui.TextInput(label="Map name", max_length=config.MAX_GUESS_LENGTH)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await self.bot.handle_guess(interaction, self.guess.value)


class GuessView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=config.ROUND_SECONDS + 60)
        self.bot = bot

    @discord.ui.button(label="Make a guess", emoji="🔍", style=discord.ButtonStyle.primary)
    async def guess_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GuessModal(self.bot))


class GuessBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())   # no privileged intents needed
        self.tree = app_commands.CommandTree(self)
        self.maps = []
        self.msgs = Messages(config.MESSAGES_FILE)
        self.rounds = {}                             # channel_id -> Round
        self.recent = {}                             # guild_id -> deque of recent map ids
        self.bad = set()                             # map ids whose image failed to download
        self.session = None

    # ---------------------------------------------------------------- setup
    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.maps = await load_maps(self.session)
        db.init()
        self.tree.add_command(make_group(self))
        self.tree.add_command(make_guess_command(self))
        dev_guild = os.environ.get("DEV_GUILD_ID")   # optional: instant command sync while testing
        if dev_guild:
            guild = discord.Object(int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.auto_rounds.start()
        self.refresh_maps.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    # --------------------------------------------------------- round logic
    async def pick_map(self, guild_id):
        recent = self.recent.setdefault(
            guild_id, deque(maxlen=min(config.RECENT_MAPS_MEMORY, max(0, len(self.maps) - 1)))
        )
        pool = [m for m in self.maps if m["id"] not in recent and m["id"] not in self.bad]
        random.shuffle(pool)
        for m in pool[:10]:                           # a few tries in case an image is dead
            path = await images.ensure_image(self.session, m)
            if path:
                recent.append(m["id"])
                return m, path
            self.bad.add(m["id"])
        return None

    async def start_round(self, channel):
        """Returns an error string, or None on success."""
        if channel.id in self.rounds:
            return "A round is already running in that channel."
        picked = await self.pick_map(channel.guild.id)
        if not picked:
            return "Couldn't fetch any map image right now (Imgur down or rate-limited?). Try again later."
        if channel.id in self.rounds:                 # someone else started one while we downloaded
            return "A round is already running in that channel."
        game_map, path = picked
        rnd = Round(channel.guild.id, channel.id, game_map, path)
        self.rounds[channel.id] = rnd
        try:
            points = "/".join(str(p) for p in config.POINTS_BY_ATTEMPT)
            text = self.msgs.pick("round_start", minutes=config.ROUND_SECONDS // 60) + (
                f"\nUse **/guess** or tap the button below. Your guesses are **private**! "
                f"Everyone gets **{config.MAX_ATTEMPTS} tries**; correct on try 1/2/3 = **{points}** points."
            )
            rnd.message = await channel.send(
                text, file=await images.blurred_file(path, 0), view=GuessView(self))
        except discord.HTTPException as e:
            self.rounds.pop(channel.id, None)
            log.warning("Couldn't post round in %s: %s", channel.id, e)
            return "I couldn't post in that channel (check my permissions)."
        rnd.timeout_task = asyncio.create_task(self._timeout(rnd, channel))
        return None

    async def _timeout(self, rnd, channel):
        await asyncio.sleep(config.ROUND_SECONDS)
        if rnd.finished:
            return
        rnd.finished = True
        if self.rounds.get(rnd.channel_id) is rnd:
            del self.rounds[rnd.channel_id]
        await self.close_view(rnd)
        await self.reveal(channel, rnd, self.msgs.pick("timeout", map_name=rnd.game_map["name"]))

    async def close_view(self, rnd):
        if rnd.message:
            try:
                await rnd.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def reveal(self, channel, rnd, text, interaction=None):
        m = rnd.game_map
        fname = f"{secrets.token_hex(6)}.jpg"
        embed = discord.Embed(title=m["name"], description=text, colour=discord.Colour.gold())
        embed.add_field(name="Creator", value=creators_text(m))
        if m.get("code"):
            embed.add_field(name="Code", value=m["code"])
        embed.set_image(url=f"attachment://{fname}")
        try:
            file = discord.File(rnd.path, filename=fname)
            if interaction:
                await interaction.response.send_message(embed=embed, file=file)   # public announcement
            else:
                await channel.send(embed=embed, file=file)
        except discord.HTTPException as e:
            log.warning("Reveal failed in %s: %s", channel.id, e)

    # ------------------------------------------------------------ guessing
    async def handle_guess(self, interaction, text):
        """Called by /guess and by the button's pop-up. Feedback is private (ephemeral);
        only a correct guess is announced publicly."""
        rnd = self.rounds.get(interaction.channel_id)
        if not rnd or rnd.finished:
            await interaction.response.send_message("There's no active round in this channel.", ephemeral=True)
            return
        text = (text or "").strip()
        if not text:
            await interaction.response.send_message("Type a map name first!", ephemeral=True)
            return

        uid = interaction.user.id
        used = rnd.attempts.get(uid, 0)
        if used >= config.MAX_ATTEMPTS:
            await interaction.response.send_message(
                "You've used all your tries this round. Cheer on the others!", ephemeral=True)
            return
        rnd.attempts[uid] = used + 1                  # count it before any await (no races)
        name = interaction.user.display_name

        try:
            if is_match(text, rnd.game_map):
                rnd.finished = True
                if rnd.timeout_task:
                    rnd.timeout_task.cancel()
                self.rounds.pop(rnd.channel_id, None)
                points = config.POINTS_BY_ATTEMPT[used]
                total = db.add_points(rnd.guild_id, uid, points)
                key = CORRECT_KEYS[min(used, len(CORRECT_KEYS) - 1)]
                msg = self.msgs.pick(key, user=name, points=points, total=total,
                                     map_name=rnd.game_map["name"])
                await self.reveal(interaction.channel, rnd, msg, interaction=interaction)
                await self.close_view(rnd)
                return

            left = config.MAX_ATTEMPTS - (used + 1)
            if left > 0:
                msg = self.msgs.pick("wrong_guess", user=name, attempts_left=left)
                file = await images.blurred_file(rnd.path, used + 1)   # less blur each try
                await interaction.response.send_message(msg, file=file, ephemeral=True)
            else:
                await interaction.response.send_message(
                    self.msgs.pick("out_of_attempts", user=name), ephemeral=True)
        except discord.HTTPException as e:
            log.warning("Guess response failed: %s", e)

    # ------------------------------------------------------ automatic rounds
    @tasks.loop(seconds=30)
    async def auto_rounds(self):
        try:
            now = int(time.time())
            for guild_id, channel_id, interval in db.due_settings(now):
                db.schedule_next(guild_id, now + interval * 60)   # schedule first: no pile-ups
                channel = self.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel) or channel.id in self.rounds:
                    continue
                err = await self.start_round(channel)
                if err:
                    log.warning("Auto round in guild %s failed: %s", guild_id, err)
        except Exception:
            log.exception("auto_rounds crashed this tick")

    @auto_rounds.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    @tasks.loop(hours=config.MAPS_REFRESH_HOURS)
    async def refresh_maps(self):
        if self.refresh_maps.current_loop == 0:        # already loaded at startup
            return
        try:
            self.maps = await load_maps(self.session)
            self.bad.clear()
        except (OSError, ValueError):
            log.exception("Map refresh failed; keeping the current list")


# ------------------------------------------------------------------ commands
def make_group(bot):
    group = app_commands.Group(name="guessmap", description="Guess the map game", guild_only=True)

    async def require_admin(interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message("You need the Manage Server permission.", ephemeral=True)
        return False

    @group.command(name="setup", description="Post a map automatically in a channel")
    @app_commands.describe(channel="Where rounds are posted", interval_minutes="Minutes between rounds")
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel,
                    interval_minutes: app_commands.Range[int, 5, 10080] = config.DEFAULT_INTERVAL_MINUTES):
        if not await require_admin(interaction):
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.attach_files and perms.embed_links and perms.read_message_history):
            await interaction.response.send_message(
                "I need Send Messages, Attach Files, Embed Links and Read Message History in that channel.",
                ephemeral=True)
            return
        db.set_schedule(interaction.guild_id, channel.id, interval_minutes, int(time.time()))
        await interaction.response.send_message(
            f"Done! A new map will appear in {channel.mention} every {interval_minutes} minutes "
            f"(first one within a minute).", ephemeral=True)

    @group.command(name="disable", description="Stop automatic rounds")
    async def disable(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        db.disable(interaction.guild_id)
        await interaction.response.send_message("Automatic rounds turned off.", ephemeral=True)

    @group.command(name="start", description="Start a round now in this channel")
    async def start(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        err = await bot.start_round(interaction.channel)
        await interaction.followup.send(err or "Round started!", ephemeral=True)

    @group.command(name="leaderboard", description="Show the top players")
    async def leaderboard(interaction: discord.Interaction):
        rows = db.top(interaction.guild_id, 10)
        if not rows:
            await interaction.response.send_message("No points yet. Be the first!")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{uid}> — **{pts}** pts ({cor} correct)"
            for i, (uid, pts, cor) in enumerate(rows)
        ]
        embed = discord.Embed(title="🏆 Guess the Map Leaderboard", description="\n".join(lines),
                              colour=discord.Colour.gold())
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="reload", description="Reload the map list and messages.json")
    async def reload(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            new_maps = await load_maps(bot.session)
            bot.msgs.load()
        except (OSError, ValueError) as e:
            await interaction.followup.send(f"Reload failed (nothing changed): {e}", ephemeral=True)
            return
        bot.maps = new_maps
        bot.bad.clear()
        await interaction.followup.send(f"Reloaded: {len(bot.maps)} maps.", ephemeral=True)

    return group


def make_guess_command(bot):
    @app_commands.command(name="guess", description="Privately guess the current map")
    @app_commands.describe(name="Your guess for the map's name")
    @app_commands.guild_only()
    async def guess(interaction: discord.Interaction, name: app_commands.Range[str, 1, config.MAX_GUESS_LENGTH]): # type: ignore
        await bot.handle_guess(interaction, name)

    return guess


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable (see README).")
    GuessBot().run(token, log_handler=None)
