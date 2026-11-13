import asyncio
import logging
import os
import random
import secrets
import time
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

import config
import db
import images
from daily import next_reset
from maps import build_suggestions, is_match, load_maps, map_display_name, suggest
from messages import Messages

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)  # .env wins over stale environment variables
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("guessmap")

CORRECT_KEYS = ["correct_first", "correct_second", "correct_third"]


@dataclass
class Round:
    guild_id: int
    channel_id: int
    game_map: dict
    path: str
    message_id: int | None = None
    used: dict = field(default_factory=dict)  # user_id -> guesses used on this map
    solved: set = field(default_factory=set)  # user_ids who got it right


class GuessModal(discord.ui.Modal, title="Guess the map"):
    guess = discord.ui.TextInput(
        label="Map name, author, or both",
        placeholder="[Map name] | by [Author]",
        max_length=config.MAX_GUESS_LENGTH,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await self.bot.handle_guess(interaction, self.guess.value)


class GuessView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(
            timeout=None
        )  # persistent: the button keeps working after restarts
        self.bot = bot

    @discord.ui.button(
        label="Make a guess",
        emoji="🔍",
        style=discord.ButtonStyle.primary,
        custom_id="guessmap:guess",
    )
    async def guess_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(GuessModal(self.bot))


class GuessBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.maps = []
        self.msgs = Messages(config.MESSAGES_FILE)
        self.rounds = {}  # channel_id -> Round (the current daily map)
        self.bad = set()  # map ids whose image failed to download
        self.next_try = {}  # channel_id -> earliest retry time after a failure
        self.session = None
        self.guess_mention = (
            "**/guess**"  # becomes a clickable </guess:id> after the commands sync
        )
        self._index = []  # autocomplete index (rebuilt when self.maps changes)
        self._index_for = None
        self.round_locks = {}  # channel_id -> asyncio.Lock
        self.round_started_at = {}  # channel_id -> timestamp of latest posted map

    # ---------------------------------------------------------------- setup
    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.maps = await load_maps(self.session)
        db.init()
        await self.restore_rounds()
        self.add_view(GuessView(self))  # re-attach the button on existing messages
        dev_guild = os.environ.get(
            "DEV_GUILD_ID"
        )  # optional: instant command sync while testing
        guild = discord.Object(int(dev_guild)) if dev_guild else None
        if (
            os.environ.get("CLEAN_COMMANDS") == "1"
        ):  # one-off: wipe stale/duplicate commands on Discord
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            if guild:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Wiped all registered commands; re-registering them now")
        self.tree.add_command(make_group(self))
        self.tree.add_command(make_guess_command(self))
        self.tree.add_command(make_pass_command(self))
        if guild:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        for cmd in synced:  # remember /guess's id so messages can link to it
            if cmd.name == "guess":
                self.guess_mention = f"</guess:{cmd.id}>"
        self.auto_rounds.start()
        self.refresh_maps.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def restore_rounds(self):
        """After a restart, pick today's maps (and everyone's progress) back up from the database."""
        by_id = {m["id"]: m for m in self.maps}
        for channel_id, guild_id, map_id, message_id in db.load_rounds():
            game_map = by_id.get(map_id)
            path = (
                await images.ensure_image(self.session, game_map) if game_map else None
            )
            if not path:
                log.warning(
                    "Dropping saved round in channel %s: map %s is unavailable",
                    channel_id,
                    map_id,
                )
                db.delete_round(channel_id)
                continue
            rnd = Round(guild_id, channel_id, game_map, path, message_id)
            for user_id, used, solved in db.load_attempts(channel_id):
                rnd.used[user_id] = used
                if solved:
                    rnd.solved.add(user_id)
            self.rounds[channel_id] = rnd
            log.info(
                "Restored the map in channel %s (%d players so far)",
                channel_id,
                len(rnd.used),
            )

    def suggestions(self):
        """Autocomplete index for the current map list (cached until the list is replaced)."""
        if self._index_for is not self.maps:
            self._index = build_suggestions(self.maps)
            self._index_for = self.maps
        return self._index

    async def map_message(self, text, path, level, title=None):
        """An embed (text + the blurred image inside it) and its file, ready to send."""
        file = await images.blurred_file(path, level)
        embed = discord.Embed(
            title=title, description=text, colour=discord.Colour.blurple()
        )
        embed.set_image(url=f"attachment://{file.filename}")
        return {"embed": embed, "file": file}

    # --------------------------------------------------------- round logic
    async def pick_map(self, guild_id):
        limit = min(config.RECENT_MAPS_MEMORY, max(0, len(self.maps) - 1))
        recent = set(db.recent_ids(guild_id, limit))
        pool = [
            m for m in self.maps if m["id"] not in recent and m["id"] not in self.bad
        ]
        if not pool:
            pool = [m for m in self.maps if m["id"] not in self.bad]
        if not pool:
            self.bad.clear()
            pool = list(self.maps)
        random.shuffle(pool)
        for m in pool[:10]:  # a few tries in case an image is dead
            path = await images.ensure_image(self.session, m)
            if path:
                db.add_recent(guild_id, m["id"])
                return m, path
            self.bad.add(m["id"])
        return None

    async def start_round(self, channel, *, requested_at=None, advance_schedule=False):
        """Safely start a round, preventing overlapping or duplicate requests."""
        lock = self.round_locks.setdefault(channel.id, asyncio.Lock())

        async with lock:
            # Another request may have already posted a map after this
            # request was submitted. Don't post a second one.
            if (
                requested_at is not None
                and self.round_started_at.get(channel.id, 0) > requested_at
            ):
                return None, False

            err = await self._start_round_locked(channel)

            if err is None and advance_schedule:
                db.schedule_next(channel.guild.id, next_reset())

            return err, err is None

    async def _start_round_locked(self, channel):
        """Start a round. Caller MUST hold the channel's round lock."""
        picked = await self.pick_map(channel.guild.id)

        if not picked:
            return "Couldn't fetch a map image right now (Imgur down or rate-limited?). Try again later."

        game_map, path = picked
        old = self.rounds.get(channel.id)

        if old:
            await self.close_round(channel, old)

        points = "/".join(str(p) for p in config.POINTS_BY_ATTEMPT)
        text = (
            self.msgs.pick("round_start")
            + f"\nClick {self.guess_mention} and start typing the map's name for suggestions, "
            "or tap the button below. Your guesses are **private**! "
            f"You get **{config.MAX_ATTEMPTS} tries**: correct on try 1/2/3 = **{points}** points."
            f"\nNext map <t:{next_reset()}:R>."
        )

        try:
            message = await channel.send(
                view=GuessView(self),
                **await self.map_message(text, path, 0, title="🗺️ Guess the map!"),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't post the map in %s: %s", channel.id, e)
            return "I couldn't post in that channel (check my permissions)."

        self.rounds[channel.id] = Round(
            channel.guild.id, channel.id, game_map, path, message.id
        )
        self.round_started_at[channel.id] = time.time()

        db.save_round(
            channel.id, channel.guild.id, game_map["id"], message.id, int(time.time())
        )

        return None

    async def retire_post(self, channel, rnd):
        """Remove the button and the now-stale 'Next map <countdown>' line from a finished map's post."""
        if not rnd.message_id:
            return
        try:
            message = await channel.fetch_message(rnd.message_id)
            changes = {"view": None}
            if message.embeds:
                embed = message.embeds[0]
                lines = [
                    l
                    for l in (embed.description or "").split("\n")
                    if not l.startswith("Next map <t:")
                ]
                embed.description = "\n".join(lines) + "\n*This map has ended.*"
                changes["embed"] = embed
            await message.edit(**changes)
        except discord.HTTPException:
            pass  # message deleted or not editable: nothing to do

    async def close_round(self, channel, rnd):
        """End a map: remove its button and publicly reveal the answer."""
        self.rounds.pop(rnd.channel_id, None)
        db.delete_round(rnd.channel_id)
        await self.retire_post(channel, rnd)
        text = self.msgs.pick(
            "day_over", map_name=map_display_name(rnd.game_map), solvers=len(rnd.solved)
        )
        await self.reveal(channel, rnd, text)

    async def reveal(self, channel, rnd, text, interaction=None, ephemeral=False):
        """Show the answer: name, creator, code and the unblurred image.
        With an interaction (+ ephemeral=True) only that player sees it."""
        m = rnd.game_map
        fname = f"{secrets.token_hex(6)}.jpg"
        embed = discord.Embed(
            title=map_display_name(m)[:256],
            description=text,
            colour=discord.Colour.gold(),
        )
        if m.get("code"):
            embed.add_field(name="Code", value=m["code"])
        embed.set_image(url=f"attachment://{fname}")
        try:
            file = discord.File(rnd.path, filename=fname)
            if interaction:
                await interaction.response.send_message(
                    embed=embed, file=file, ephemeral=ephemeral
                )
            else:
                await channel.send(embed=embed, file=file)
        except discord.HTTPException as e:
            log.warning("Reveal failed in %s: %s", rnd.channel_id, e)

    # ------------------------------------------------------------ guessing
    async def _say(self, interaction, text):
        """A private (only-you-see-it) reply."""
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException as e:
            log.warning("Reply failed: %s", e)

    async def _announce(self, channel, name, attempt, points):
        """Public 'X solved it!' message that doesn't reveal the answer."""
        if (
            not config.ANNOUNCE_CORRECT
            or channel is None
            or "announce_correct" not in self.msgs.data
        ):
            return
        try:
            await channel.send(
                self.msgs.pick(
                    "announce_correct", user=name, attempt=attempt, points=points
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("Announcement failed: %s", e)

    async def _player_state(self, interaction):
        """Checks shared by /guess and /pass. Returns (round, tries used) if this player can still
        play; otherwise replies privately and returns None."""
        rnd = self.rounds.get(interaction.channel_id)
        if not rnd:
            await self._say(
                interaction, "There's no active map in this channel right now."
            )
            return None
        uid = interaction.user.id
        if uid in rnd.solved:
            await self._say(
                interaction,
                f"You already solved this one! The next map appears <t:{next_reset()}:R>.",
            )
            return None
        used = rnd.used.get(uid, 0)
        if used >= config.MAX_ATTEMPTS:
            await self._say(
                interaction,
                f"You've used all your tries on this map. The next one appears <t:{next_reset()}:R>.",
            )
            return None
        return rnd, used

    async def _show_clearer(self, interaction, rnd, used):
        """Private reply with the next, less blurry image (`used` = tries this player has now used)."""
        left = config.MAX_ATTEMPTS - used
        msg = self.msgs.pick(
            "wrong_guess", user=interaction.user.display_name, attempts_left=left
        )
        msg += f"\nGuess again: {self.guess_mention}"
        await interaction.response.send_message(
            ephemeral=True, **await self.map_message(msg, rnd.path, used)
        )

    async def handle_pass(self, interaction):
        """/pass: spend a try without guessing, to see a clearer image."""
        state = await self._player_state(interaction)
        if not state:
            return
        rnd, used = state
        if used + 1 >= config.MAX_ATTEMPTS:
            await self._say(
                interaction, "That's your last try, so you can't pass it. Take a guess!"
            )
            return
        uid = interaction.user.id
        rnd.used[uid] = used + 1  # count it before any await (no races)
        db.set_attempt(rnd.channel_id, uid, used + 1, False)
        try:
            await self._show_clearer(interaction, rnd, used + 1)
        except discord.HTTPException as e:
            log.warning("Pass response failed: %s", e)

    async def handle_guess(self, interaction, text):
        """Called by /guess and by the button's pop-up. Everything the player sees is private."""
        state = await self._player_state(interaction)
        if not state:
            return
        rnd, used = state
        text = (text or "").strip()
        if not text:
            await self._say(interaction, "Type a map name first!")
            return
        if len(text) > config.MAX_GUESS_LENGTH:
            await self._say(
                interaction,
                f"That's too long for a map name (max {config.MAX_GUESS_LENGTH} characters).",
            )
            return

        uid = interaction.user.id
        correct = is_match(text, rnd.game_map)
        rnd.used[uid] = used + 1  # count it before any await (no races)
        if correct:
            rnd.solved.add(uid)
        db.set_attempt(rnd.channel_id, uid, used + 1, correct)
        name = interaction.user.display_name
        map_name = map_display_name(rnd.game_map)

        try:
            if correct:
                points = config.POINTS_BY_ATTEMPT[used]
                total = db.add_points(rnd.guild_id, uid, points)
                key = CORRECT_KEYS[min(used, len(CORRECT_KEYS) - 1)]
                msg = self.msgs.pick(
                    key, user=name, points=points, total=total, map_name=map_name
                )
                # the answer is shown ONLY to the person who guessed it
                await self.reveal(
                    interaction.channel,
                    rnd,
                    msg,
                    interaction=interaction,
                    ephemeral=True,
                )
                await self._announce(interaction.channel, name, used + 1, points)
                return

            left = config.MAX_ATTEMPTS - (used + 1)
            if left > 0:
                await self._show_clearer(
                    interaction, rnd, used + 1
                )  # less blur each try
            else:
                # final miss: reveal the map, but only to this player
                msg = self.msgs.pick("out_of_attempts", user=name, map_name=map_name)
                await self.reveal(
                    interaction.channel,
                    rnd,
                    msg,
                    interaction=interaction,
                    ephemeral=True,
                )
        except discord.HTTPException as e:
            log.warning("Guess response failed: %s", e)

    # ------------------------------------------------------ automatic maps
    @tasks.loop(seconds=5)
    async def auto_rounds(self):
        try:
            for _, channel_id, _ in db.all_settings():
                channel = self.get_channel(channel_id)

                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.HTTPException:
                        log.exception(
                            "Couldn't fetch configured channel %s", channel_id
                        )
                        continue

                if not isinstance(channel, discord.TextChannel):
                    continue

                lock = self.round_locks.setdefault(channel_id, asyncio.Lock())

                async with lock:
                    # Re-read settings AFTER acquiring the lock.
                    setting = next(
                        (row for row in db.all_settings() if row[1] == channel_id),
                        None,
                    )

                    if setting is None:
                        continue

                    guild_id, _, next_at = setting

                    now = int(time.time())
                    due = next_at <= now

                    # Re-check whether a map is actually needed.
                    if not due and (
                        channel_id in self.rounds
                        or self.next_try.get(channel_id, 0) > now
                    ):
                        continue

                    err = await self._start_round_locked(channel)

                    if err:
                        log.warning("Daily map in guild %s failed: %s", guild_id, err)
                        self.next_try[channel_id] = now + 600

                        if due:
                            db.schedule_next(guild_id, now + 600)

                    else:
                        self.next_try.pop(channel_id, None)

                        if due:
                            db.schedule_next(guild_id, next_reset(now))

        except Exception:  # noqa: BLE001
            log.exception("auto_rounds crashed this tick")

    @auto_rounds.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    @tasks.loop(hours=config.MAPS_REFRESH_HOURS)
    async def refresh_maps(self):
        if self.refresh_maps.current_loop == 0:  # already loaded at startup
            return
        try:
            self.maps = await load_maps(self.session)
            self.bad.clear()
        except (OSError, ValueError):
            log.exception("Map refresh failed; keeping the current list")


# ------------------------------------------------------------------ commands
def make_group(bot):
    group = app_commands.Group(
        name="guessmap", description="Guess the map game", guild_only=True
    )

    async def require_admin(interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "You need the Manage Server permission.", ephemeral=True
        )
        return False

    @group.command(name="setup", description="Post new maps on a schedule in a channel")
    @app_commands.describe(channel="Where the daily map is posted")
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction):
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (
            perms.send_messages
            and perms.attach_files
            and perms.embed_links
            and perms.read_message_history
        ):
            await interaction.response.send_message(
                "I need Send Messages, Attach Files, Embed Links and Read Message History in that channel.",
                ephemeral=True,
            )
            return
        fresh = channel.id not in bot.rounds
        db.set_schedule(
            interaction.guild_id,
            channel.id,
            1440,
            int(time.time()) if fresh else next_reset(),
        )
        first = (
            "the first one appears within a minute"
            if fresh
            else "the current map stays until then"
        )
        hour = f"{config.DAILY_RESET_HOUR_UTC:02d}:00 UTC"
        minutes = round(config.MAP_INTERVAL_HOURS * 60)
        if minutes == 1440:
            every = f"every day at {hour}"
        elif minutes % 60 == 0:
            every = f"every {minutes // 60} hours (counted from {hour})"
        else:
            every = f"every {minutes} minutes"
        await interaction.response.send_message(
            f"Done! A new map will appear in {channel.mention} {every} ({first}). "
            "Use `/guessmap newmap` any time to skip ahead.",
            ephemeral=True,
        )

    @group.command(name="disable", description="Stop the daily maps")
    async def disable(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        db.disable(interaction.guild_id)
        await interaction.response.send_message(
            "Daily maps turned off.", ephemeral=True
        )

    @group.command(
        name="newmap",
        description="Admin: skip ahead and post a brand-new map right now",
    )
    async def newmap(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        err, posted = await bot.start_round(
            interaction.channel,
            requested_at=interaction.created_at.timestamp(),
            advance_schedule=True,
        )

        if err:
            response = err
        elif posted:
            response = "New map posted! The previous answer was revealed."
        else:
            response = (
                "A new map was already posted while this command was being processed."
            )

        await interaction.followup.send(response, ephemeral=True)

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
        embed = discord.Embed(
            title="🏆 Guess the Map Leaderboard",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @group.command(name="reload", description="Reload the map list and messages.json")
    async def reload(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(
            ephemeral=True
        )  # fetching can take longer than 3 seconds
        try:
            new_maps = await load_maps(bot.session)
            bot.msgs.load()
        except (OSError, ValueError) as e:
            await interaction.followup.send(
                f"Reload failed (nothing changed): {e}", ephemeral=True
            )
            return
        bot.maps = new_maps
        bot.bad.clear()
        await interaction.followup.send(
            f"Reloaded: {len(bot.maps)} maps.", ephemeral=True
        )

    return group


def make_guess_command(bot):
    @app_commands.command(
        name="guess",
        description="Privately guess today's map by name or author"
    )
    @app_commands.describe(
        answer="Map name, map author, or both (e.g. Radiant Growth | by Bojack)"
    )
    @app_commands.guild_only()
    async def guess(interaction: discord.Interaction, answer: str):
        await bot.handle_guess(interaction, answer)

    @guess.autocomplete("answer")
    async def guess_autocomplete(interaction: discord.Interaction, current: str):
        rnd = bot.rounds.get(interaction.channel_id)

        if (
            not rnd
            or interaction.user.id in rnd.solved
            or rnd.used.get(interaction.user.id, 0) >= config.MAX_ATTEMPTS
        ):
            return []

        names = suggest(
            bot.suggestions(),
            current,
            config.AUTOCOMPLETE_MAX_RESULTS
        )

        return [
            app_commands.Choice(name=n, value=n)
            for n in names
        ]

    return guess


def make_pass_command(bot):
    @app_commands.command(
        name="pass",
        description="Skip a guess to see a less blurry image (uses up a try)",
    )
    @app_commands.guild_only()
    async def pass_(interaction: discord.Interaction):
        await bot.handle_pass(interaction)

    return pass_


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable (see README).")
    GuessBot().run(token, log_handler=None)
