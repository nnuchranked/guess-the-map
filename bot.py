import asyncio
import calendar
import json
import logging
import os
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

import config
import db
import images
from daily import next_reset
from maps import build_suggestions, creators_text, is_match, load_maps, suggest
from messages import Messages

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)                         # .env wins over stale environment variables
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("guessmap")

CORRECT_KEYS = ["correct_first", "correct_second", "correct_third"]
ALREADY_FINISHED_CHOICE = "__GUESSMAP_ALREADY_FINISHED__"
NO_ACTIVE_MAP_CHOICE = "__GUESSMAP_NO_ACTIVE_MAP__"
START_FIRST_CHOICE = "__GUESSMAP_START_FIRST__"
MODE_ALREADY_FINISHED_CHOICE = "__GUESSMAP_MODE_FINISHED__"
MODE_NO_CHALLENGE_CHOICE = "__GUESSMAP_MODE_NO_CHALLENGE__"
MODE_START_FIRST_CHOICE = "__GUESSMAP_MODE_START_FIRST__"
INFINITE_NO_SESSION_CHOICE = "__GUESSMAP_INFINITE_NO_SESSION__"
INFINITE_FINISHED_CHOICE = "__GUESSMAP_INFINITE_FINISHED__"
CHALLENGE_MODES = ("easy", "normal", "hard", "impossible", "zoom")
DIFFICULTY_MODES = ("easy", "normal", "hard", "impossible")


def configure_view_buttons(view, bot, labels):
    """Apply messages.json labels to button custom IDs on a view instance."""
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.custom_id in labels:
            item.label = bot.msgs.text(labels[item.custom_id])[:80]


# Speed bonus: +0.5 initially, decreasing by 0.1 every six seconds; zero at 30s.
SPEED_BONUS_MAX = getattr(config, "SPEED_BONUS_MAX", 0.5)
SPEED_BONUS_INTERVAL_SECONDS = getattr(config, "SPEED_BONUS_INTERVAL_SECONDS", 6)
SPEED_BONUS_STEP = getattr(config, "SPEED_BONUS_STEP", 0.1)
SPEED_BONUS_CUTOFF_SECONDS = getattr(config, "SPEED_BONUS_CUTOFF_SECONDS", 30)
LEADERBOARD_PAGE_SIZE = max(1, min(25, int(getattr(config, "LEADERBOARD_PAGE_SIZE", 20))))


@dataclass
class Round:
    guild_id: int
    channel_id: int
    game_map: dict
    path: str
    message_id: int | None = None
    started_at: int = 0                            # persisted round identity for per-player stats
    used: dict = field(default_factory=dict)     # user_id -> guesses used on this map
    solved: set = field(default_factory=set)     # user_ids who got it right
    attempt_started_at: dict = field(default_factory=dict)  # user_id -> monotonic start time
    player_started_at: dict = field(default_factory=dict)   # user_id -> first reveal time (epoch ms)
    starting: set = field(default_factory=set)              # prevents double-click timer resets


class GuessView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)            # persistent: the button keeps working after restarts
        self.bot = bot
        configure_view_buttons(self, bot, {"guessmap:guess": "buttons.start_guessing"})

    @discord.ui.button(label="…", emoji="▶️", style=discord.ButtonStyle.primary,
                       custom_id="guessmap:guess")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_guessing(interaction)


class PlayView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        configure_view_buttons(self, bot, {"guessmap:play": "buttons.play_yourself"})

    @discord.ui.button(label="…", emoji="🗺️", style=discord.ButtonStyle.primary,
                       custom_id="guessmap:play")
    async def play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_guessing(interaction)


class DifficultyChallengeView(discord.ui.View):
    """Persistent buttons for the four daily difficulty variants."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        configure_view_buttons(self, bot, {
            "guessmap:difficulty:easy": "buttons.easy",
            "guessmap:difficulty:normal": "buttons.normal",
            "guessmap:difficulty:hard": "buttons.hard",
            "guessmap:difficulty:impossible": "buttons.impossible",
        })

    @discord.ui.button(label="…", emoji="🟢", style=discord.ButtonStyle.success,
                       custom_id="guessmap:difficulty:easy")
    async def easy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_challenge(interaction, "easy")

    @discord.ui.button(label="…", emoji="🟡", style=discord.ButtonStyle.primary,
                       custom_id="guessmap:difficulty:normal")
    async def normal_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_challenge(interaction, "normal")

    @discord.ui.button(label="…", emoji="🔴", style=discord.ButtonStyle.danger,
                       custom_id="guessmap:difficulty:hard")
    async def hard_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_challenge(interaction, "hard")

    @discord.ui.button(label="…", emoji="💀",
                       style=discord.ButtonStyle.secondary,
                       custom_id="guessmap:difficulty:impossible")
    async def impossible_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_challenge(interaction, "impossible")


class ZoomChallengeView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        configure_view_buttons(self, bot, {"guessmap:challenge:zoom": "buttons.start_zoom_challenge"})

    @discord.ui.button(label="…", emoji="🔎", style=discord.ButtonStyle.primary,
                       custom_id="guessmap:challenge:zoom")
    async def zoom_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.begin_challenge(interaction, "zoom")


@dataclass
class ScheduledChallenge:
    guild_id: int
    kind: str
    challenge_key: str
    channel_id: int
    game_map: dict
    path: str
    message_id: int | None = None


@dataclass
class InfiniteSession:
    guild_id: int
    user_id: int
    game_map: dict
    path: str
    effects: dict
    effect_labels: list[str]
    guessed: bool = False
    message: object | None = None       # current ephemeral map/result message, when editable


class InfiniteView(discord.ui.View):
    """User-scoped controls for the unscored infinite random-filter mode."""
    def __init__(self, bot, owner_id, *, completed=False, session=None):
        super().__init__(timeout=1800)
        self.bot = bot
        self.owner_id = owner_id
        self.session = session
        # Before a guess the button reveals the answer; afterwards it advances.
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.label = bot.msgs.text(
                    "infinite.button_next" if completed else "infinite.button_skip"
                )[:80]
                item.emoji = "🎲" if completed else "⏭️"

    @discord.ui.button(label="…", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def next_map(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await self.bot.send_text(
                interaction, self.t("infinite.session_owner_error"),
                title=self.t("infinite.session_title"), ephemeral=True, colour=discord.Colour.red(),
            )
            return

        active = self.bot.infinite_sessions.get((interaction.guild_id, self.owner_id))
        if active is None or (self.session is not None and active is not self.session):
            await self.bot.send_text(
                interaction, self.bot.t("infinite.expired_body"),
                title=self.bot.t("infinite.expired_title"), ephemeral=True, colour=discord.Colour.orange(),
            )
            return

        if active.guessed:
            await self.bot.start_infinite(interaction, owner_id=self.owner_id, edit_message=True)
        else:
            await self.bot.skip_infinite(interaction, owner_id=self.owner_id)


class GuessBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.maps = []
        self.msgs = Messages(config.MESSAGES_FILE)
        self.rounds = {}                              # channel_id -> Round (the current daily map)
        self.bad = set()                              # map ids whose image failed to download
        self.next_try = {}                            # channel_id -> earliest retry time after a failure
        self.round_locks = {}                         # channel_id -> asyncio.Lock
        self.map_pick_locks = {}                      # guild_id -> asyncio.Lock
        self.session = None
        self.guess_mention = "**/guess**"             # becomes a clickable </guess:id> after the commands sync
        self._index = []                              # autocomplete index (rebuilt when self.maps changes)
        self._index_for = None
        self.leaderboard_posted = self._load_leaderboard_state()
        self.scheduled_challenges = {}                # (guild_id, kind) -> ScheduledChallenge
        self.mode_timers = {}                         # (guild, challenge key, mode, user) -> monotonic time
        self.mode_starting = set()                    # prevents duplicate start-button requests
        self.infinite_sessions = {}                    # (guild_id, user_id) -> unscored InfiniteSession

    # ---------------------------------------------------------------- setup
    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.maps = await load_maps(self.session)
        db.init()
        await self.restore_rounds()
        await self.restore_challenges()
        self.add_view(GuessView(self))                # re-attach persistent buttons after restarts
        self.add_view(PlayView(self))
        self.add_view(DifficultyChallengeView(self))
        self.add_view(ZoomChallengeView(self))
        dev_guild = os.environ.get("DEV_GUILD_ID")    # optional: instant command sync while testing
        guild = discord.Object(int(dev_guild)) if dev_guild else None
        if os.environ.get("CLEAN_COMMANDS") == "1":   # one-off: wipe stale/duplicate commands on Discord
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            if guild:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Wiped all registered commands; re-registering them now")
        self.tree.add_command(make_group(self))
        self.tree.add_command(make_guess_command(self))
        self.tree.add_command(make_pass_command(self))
        self.tree.add_command(make_guesschallenge_command(self))
        self.tree.add_command(make_guessinfinite_command(self))
        if guild:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        for cmd in synced:                            # remember /guess's id so messages can link to it
            if cmd.name == "guess":
                self.guess_mention = f"</guess:{cmd.id}>"
        self.auto_rounds.start()
        self.daily_leaderboards.start()
        self.weekly_leaderboards.start()
        self.scheduled_challenge_loop.start()
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
        for channel_id, guild_id, map_id, message_id, started_at in db.load_rounds():
            game_map = by_id.get(map_id)
            path = await images.ensure_image(self.session, game_map) if game_map else None
            if not path:
                log.warning("Dropping saved round in channel %s: map %s is unavailable", channel_id, map_id)
                db.delete_round(channel_id)
                continue
            rnd = Round(guild_id, channel_id, game_map, path, message_id, started_at)
            for user_id, used, solved in db.load_attempts(channel_id):
                rnd.used[user_id] = used
                if solved:
                    rnd.solved.add(user_id)
            rnd.player_started_at.update(db.load_map_start_times(channel_id, started_at))
            self.rounds[channel_id] = rnd
            # Migrate old posts that publicly attached the map. New-format posts are
            # plain text already, so leave their layout alone and just reattach the view.
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    message = await channel.fetch_message(message_id)
                    embed = message.embeds[0] if message.embeds else discord.Embed(
                        title=self.t("main.round_post_title"), colour=discord.Colour.blurple()
                    )
                    embed.title = self.t("main.round_post_title")
                    embed.description = self.t(
                        "main.restore_post_description",
                        start_button=self.t("buttons.start_guessing"), guess_command=self.guess_mention,
                    )
                    embed.set_image(url=None)
                    await message.edit(content=None, embed=embed, attachments=[], view=GuessView(self))
            except (discord.HTTPException, AttributeError):
                log.warning("Could not restore the daily map post in channel %s", channel_id)
            log.info("Restored the map in channel %s (%d players so far)", channel_id, len(rnd.used))

    async def restore_challenges(self):
        """Restore the currently scheduled daily difficulty and zoom-out challenges."""
        by_id = {m["id"]: m for m in self.maps}
        for guild_id, kind, challenge_key, channel_id, map_id, message_id, _created_at in db.all_scheduled_challenges():
            game_map = by_id.get(map_id)
            path = await images.ensure_image(self.session, game_map) if game_map else None
            if not path:
                log.warning("Could not restore %s challenge for guild %s", kind, guild_id)
                continue
            challenge = ScheduledChallenge(
                guild_id, kind, challenge_key, channel_id, game_map, path, message_id
            )
            self.scheduled_challenges[(guild_id, kind)] = challenge

            # Upgrade already-posted challenge announcements to embed-only layout.
            # The persistent view registered in setup_hook restores their buttons.
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                if isinstance(channel, discord.TextChannel) and message_id:
                    message = await channel.fetch_message(message_id)
                    if kind == "difficulty":
                        title = self.t("challenge.difficulty_announcement_title", challenge_key=challenge_key)
                        view = DifficultyChallengeView(self)
                        description = self.t(
                            "challenge.difficulty_announcement", rewards=self.challenge_rewards_text()
                        )
                    else:
                        title = self.t("challenge.zoom_announcement_title", challenge_key=challenge_key)
                        view = ZoomChallengeView(self)
                        description = self.t(
                            "challenge.zoom_announcement", attempts=config.MAX_ATTEMPTS
                        )
                    embed = message.embeds[0] if message.embeds else self.text_embed(
                        message.content or description, title=title
                    )
                    embed.title = title
                    embed.description = description
                    embed.set_image(url=None)
                    await message.edit(content=None, embed=embed, attachments=[], view=view)
            except (discord.HTTPException, AttributeError):
                log.warning("Could not upgrade %s challenge post in channel %s", kind, channel_id)

    def t(self, key, default=None, **values):
        """Fetch and format user-facing text from messages.json."""
        return self.msgs.text(key, default=default, **values)

    def text_embed(self, text, title=None, colour=None):
        """Build a consistently formatted embed for every text-only bot response."""
        description = str(text or "").strip() or "\u200b"
        if len(description) > 4096:
            description = description[:4090].rstrip() + "…"
        title = (title or self.t("common.default_embed_title"))[:256]
        embed = discord.Embed(
            title=title,
            description=description,
            colour=colour or discord.Colour.blurple(),
        )
        return embed

    async def send_text(self, interaction, text, *, title=None, ephemeral=True, colour=None):
        """Send text-only interaction feedback as an embed, even after a defer."""
        embed = self.text_embed(text, title=title, colour=colour)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

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
            title=(title or self.t("main.private_map_title"))[:256],
            description=str(text)[:4096], colour=discord.Colour.blurple(),
        )
        embed.set_image(url=f"attachment://{file.filename}")
        return {"embed": embed, "file": file}

    def _leaderboard_state_path(self):
        return getattr(config, "DAILY_LEADERBOARD_STATE_FILE", "daily_leaderboard_state.json")

    def _load_leaderboard_state(self):
        try:
            with open(self._leaderboard_state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_leaderboard_state(self):
        path = self._leaderboard_state_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.leaderboard_posted, f, indent=2)
        os.replace(temp_path, path)

    def _leaderboard_lines(self, rows, start_rank=1):
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for index, row in enumerate(rows):
            uid, points, correct, played, solved, avg_guesses = row
            rank = start_rank + index
            rank_label = medals[rank - 1] if rank <= 3 else f"**{rank}.**"
            if played:
                performance = self.t(
                    "leaderboard.solved_performance", solved=solved, played=played,
                    rate=solved / played * 100,
                )
            else:
                performance = self.t("leaderboard.solved_not_tracked")
            average = (
                self.t("leaderboard.avg_attempts", average=avg_guesses)
                if avg_guesses is not None
                else self.t("leaderboard.avg_not_available")
            )
            lines.append(self.t(
                "leaderboard.row", rank_label=rank_label, user_id=uid,
                points=f"{points:g}", correct=correct, performance=performance, average=average,
            ))
        return lines

    def leaderboard_embed(self, guild_id, rows=None, page=0, page_size=LEADERBOARD_PAGE_SIZE,
                          full=False, mode=None):
        if rows is None:
            rows = db.top_mode(guild_id, mode, None if full else 10) if mode else db.top(
                guild_id, None if full else 10
            )
        total = len(rows)
        start = page * page_size
        page_rows = rows[start:start + page_size]
        if not rows:
            description = self.t("leaderboard.empty")
        else:
            description = "\n".join(self._leaderboard_lines(page_rows, start + 1))
        if mode:
            mode_label = self.mode_title(mode)
            title = self.t("leaderboard.challenge_title", mode_label=mode_label)
        else:
            title = self.t("leaderboard.main_title")
        if full:
            title += self.t("leaderboard.full_suffix")
        embed = discord.Embed(title=title, description=description, colour=discord.Colour.gold())
        if full and total > page_size:
            embed.set_footer(text=self.t(
                "leaderboard.page_footer", page=page + 1,
                pages=(total + page_size - 1) // page_size, total=total,
            ))
        elif full:
            embed.set_footer(text=self.t("leaderboard.all_footer", total=total))
        else:
            embed.set_footer(text=self.t("leaderboard.top_footer"))
        return embed

    def weekly_leaderboard_embed(self, guild_id, week_key=None, rows=None, page=0,
                                 page_size=LEADERBOARD_PAGE_SIZE, full=False):
        week_key = week_key or db.current_week_key()
        if rows is None:
            rows = db.top_weekly(guild_id, week_key, None if full else 10)
        total = len(rows)
        start = page * page_size
        page_rows = rows[start:start + page_size]
        if not rows:
            description = self.t("weekly.empty", week=week_key)
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for index, (uid, points, correct) in enumerate(page_rows):
                rank = start + index + 1
                rank_label = medals[rank - 1] if rank <= 3 else f"**{rank}.**"
                lines.append(self.t(
                    "weekly.row", rank_label=rank_label, user_id=uid,
                    points=f"{points:g}", correct=correct,
                ))
            description = "\n".join(lines)
        title = self.t("weekly.title", week=week_key)
        if full:
            title += self.t("leaderboard.full_suffix")
        embed = discord.Embed(title=title[:256], description=description[:4096], colour=discord.Colour.gold())
        if full and total > page_size:
            embed.set_footer(text=self.t(
                "weekly.page_footer", page=page + 1,
                pages=(total + page_size - 1) // page_size, total=total, week=week_key,
            ))
        elif full:
            embed.set_footer(text=self.t("weekly.all_footer", total=total, week=week_key))
        else:
            embed.set_footer(text=self.t("weekly.top_footer", week=week_key))
        return embed

    def player_profile_embed(self, guild_id, member):
        profile = db.player_profile(guild_id, member.id)
        played = profile["played"]
        solved = profile["solved"]
        rate = (solved / played * 100.0) if played else 0.0
        average_guesses = profile["average_guesses"]
        average_time = profile["average_solve_time_ms"]
        main_value = self.t(
            "profile.main_value", points=f"{profile['points']:g}", correct=profile["correct"],
            played=played, solved=solved, rate=rate,
            average_guesses=(f"{average_guesses:.2f}" if average_guesses is not None else self.t("profile.na")),
            average_time=(f"{average_time / 1000:.1f}s" if average_time is not None else self.t("profile.na")),
        )
        weekly_value = self.t(
            "profile.weekly_value",
            week=profile["week_key"],
            weekly_points=f"{profile['weekly_points']:g}",
            weekly_correct=profile["weekly_correct"],
        )
        mode_lines = []
        for mode in CHALLENGE_MODES:
            score = profile["mode_scores"].get(mode)
            stats = profile["mode_stats"].get(mode, {"played": 0, "solved": 0})
            if not score and not stats["played"]:
                continue
            score = score or {"points": 0.0, "correct": 0}
            mode_lines.append(self.t(
                "profile.challenge_row", mode=self.mode_title(mode),
                points=f"{score['points']:g}", correct=score["correct"],
                solved=stats["solved"], played=stats["played"],
            ))
        challenge_value = "\n".join(mode_lines) if mode_lines else self.t("profile.no_challenges")
        infinite_value = self.t(
            "profile.infinite_value", current=profile["infinite_current"],
            best=profile["infinite_best"],
        )
        embed = discord.Embed(
            title=self.t("profile.title", display_name=member.display_name)[:256],
            description=self.t("profile.description"),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name=self.t("profile.main_label"), value=main_value[:1024], inline=False)
        embed.add_field(name=self.t("profile.weekly_label"), value=weekly_value[:1024], inline=True)
        embed.add_field(name=self.t("profile.infinite_label"), value=infinite_value[:1024], inline=True)
        embed.add_field(name=self.t("profile.challenge_label"), value=challenge_value[:1024], inline=False)
        return embed

    def infinite_streak_leaderboard_embed(self, guild_id, rows=None, page=0,
                                          page_size=LEADERBOARD_PAGE_SIZE, full=False):
        if rows is None:
            rows = db.top_infinite_streaks(guild_id, None if full else 10)
        total = len(rows)
        start = page * page_size
        page_rows = rows[start:start + page_size]
        if not rows:
            description = self.t("infinite.leaderboard_empty")
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for index, (uid, current, best) in enumerate(page_rows):
                rank = start + index + 1
                rank_label = medals[rank - 1] if rank <= 3 else f"**{rank}.**"
                lines.append(self.t(
                    "infinite.leaderboard_row", rank_label=rank_label, user_id=uid,
                    current=current, best=best,
                ))
            description = "\n".join(lines)
        title = self.t("infinite.leaderboard_title")
        if full:
            title += self.t("leaderboard.full_suffix")
        embed = discord.Embed(title=title, description=description, colour=discord.Colour.gold())
        if full and total > page_size:
            embed.set_footer(text=self.t(
                "infinite.leaderboard_page_footer", page=page + 1,
                pages=(total + page_size - 1) // page_size, total=total,
            ))
        elif full:
            embed.set_footer(text=self.t("infinite.leaderboard_all_footer", total=total))
        else:
            embed.set_footer(text=self.t("infinite.leaderboard_top_footer"))
        return embed

    async def begin_guessing(self, interaction):
        """Privately reveal the current map and start this player's per-attempt timer."""
        rnd = self.rounds.get(interaction.channel_id)
        if not rnd:
            await self._say(interaction, self.t("main.no_active_map"))
            return

        uid = interaction.user.id
        if uid in rnd.solved:
            await self._say(interaction, self.t("main.already_solved"))
            return
        used = rnd.used.get(uid, 0)
        if used >= config.MAX_ATTEMPTS:
            await self._say(interaction, self.t("main.all_tries_used"))
            return
        if uid in rnd.attempt_started_at or uid in rnd.starting:
            await self._say(interaction, self.t("main.timer_running", guess_command=self.guess_mention))
            return
        rnd.starting.add(uid)

        points = "/".join(str(p) for p in config.POINTS_BY_ATTEMPT)
        text = self.t(
            "main.private_map_description",
            guess_command=self.guess_mention, attempts_left=config.MAX_ATTEMPTS - used,
            points=points, bonus_interval=SPEED_BONUS_INTERVAL_SECONDS,
            bonus_cutoff=SPEED_BONUS_CUTOFF_SECONDS,
        )
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            payload = await self.map_message(text, rnd.path, used, title=self.t("main.private_map_title"))
            await interaction.followup.send(ephemeral=True, **payload)
            # Track first reveal time for the end-of-day recap without counting a
            # map as played until the player submits a guess or uses /pass.
            started_playing_at = db.start_map_participation(
                rnd.guild_id, uid, rnd.channel_id, rnd.started_at,
                rnd.game_map["id"], int(time.time() * 1000),
            )
            rnd.player_started_at[uid] = started_playing_at
            rnd.attempt_started_at[uid] = time.monotonic()
        except discord.HTTPException as e:
            log.warning("Could not privately show map in channel %s: %s", interaction.channel_id, e)
        finally:
            rnd.starting.discard(uid)

    @staticmethod
    def speed_bonus(elapsed):
        if elapsed >= SPEED_BONUS_CUTOFF_SECONDS:
            return 0.0
        interval = max(1, SPEED_BONUS_INTERVAL_SECONDS)
        steps = int(max(0.0, elapsed) // interval)
        bonus = SPEED_BONUS_MAX - steps * SPEED_BONUS_STEP
        return round(max(0.0, bonus), 2)

    @staticmethod
    def mode_kind(mode):
        return "zoom" if mode == "zoom" else "difficulty"

    def mode_title(self, mode):
        return self.t(f"modes.{mode}.label", default=mode.title())

    def challenge_points(self, mode):
        defaults = {"easy": 1, "normal": 2, "hard": 3, "impossible": 5}
        configured = getattr(config, "DIFFICULTY_CHALLENGE_POINTS", defaults)
        return float(configured.get(mode, defaults[mode]))

    def challenge_rewards_text(self):
        modes = ("easy", "normal", "hard", "impossible")
        return " · ".join(
            f"{self.t(f'modes.{mode}.name')} **{self.challenge_points(mode):g}**"
            for mode in modes
        )

    def get_challenge(self, guild_id, mode):
        return self.scheduled_challenges.get((guild_id, self.mode_kind(mode)))

    async def pick_infinite_map(self, guild_id, exclude_map_id=None):
        """Pick a random fun-mode map without touching shared recent-map history."""
        lock = self.map_pick_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            pool = [
                game_map for game_map in self.maps
                if game_map["id"] not in self.bad and game_map["id"] != exclude_map_id
            ]
            if not pool:
                pool = [game_map for game_map in self.maps if game_map["id"] != exclude_map_id]
            if not pool:
                self.bad.clear()
                pool = list(self.maps)
            random.shuffle(pool)
            for game_map in pool[:20]:
                path = await images.ensure_image(self.session, game_map)
                if path:
                    return game_map, path
                self.bad.add(game_map["id"])
        return None

    def infinite_streak_text(self, guild_id, user_id):
        current, best = db.get_infinite_streak(guild_id, user_id)
        return self.t("infinite.streak_status", current=current, best=best)

    async def start_infinite(self, interaction, owner_id=None, edit_message=False):
        """Start or advance one private, unscored random-filter map."""
        uid = interaction.user.id
        if owner_id is not None and uid != owner_id:
            await self.send_text(
                interaction, self.t("infinite.session_owner_error"),
                title=self.t("infinite.session_title"), ephemeral=True, colour=discord.Colour.red(),
            )
            return

        key = (interaction.guild_id, uid)
        previous = self.infinite_sessions.get(key)
        exclude_id = previous.game_map["id"] if previous else None
        try:
            if edit_message:
                # Component interactions should defer an update to the existing ephemeral message.
                await interaction.response.defer()
            else:
                await interaction.response.defer(ephemeral=True, thinking=True)
            picked = await self.pick_infinite_map(interaction.guild_id, exclude_id)
            if not picked:
                await interaction.followup.send(
                    embed=self.text_embed(
                        self.t("infinite.load_failed", next_button=self.t("infinite.button_next")),
                        title=self.t("infinite.load_failed_title"), colour=discord.Colour.red(),
                    ),
                    ephemeral=True,
                )
                return

            game_map, path = picked
            effects = images.random_infinite_effects()
            labels = images.describe_infinite_effects(effects, self.t)
            file = await images.infinite_file(path, effects)
            embed = discord.Embed(
                title=self.t("infinite.title"),
                description=self.t(
                    "infinite.description",
                    skip_button=self.t("infinite.button_skip"),
                    next_button=self.t("infinite.button_next"),
                ),
                colour=discord.Colour.blurple(),
            )
            embed.add_field(
                name=self.t("infinite.streak_field"),
                value=self.infinite_streak_text(interaction.guild_id, uid),
                inline=False,
            )
            embed.add_field(
                name=self.t("infinite.filters_field"),
                value="\n".join(f"• {label}" for label in labels), inline=False,
            )
            embed.set_image(url=f"attachment://{file.filename}")
            embed.set_footer(text=self.t("infinite.footer"))
            session = InfiniteSession(
                interaction.guild_id, uid, game_map, path, effects, labels
            )
            self.infinite_sessions[key] = session
            view = InfiniteView(self, uid, completed=False, session=session)
            if edit_message:
                session.message = await interaction.edit_original_response(
                    embed=embed, attachments=[file], view=view,
                )
            else:
                session.message = await interaction.followup.send(
                    embed=embed, file=file, view=view, ephemeral=True, wait=True,
                )
        except (discord.HTTPException, OSError, ValueError) as exc:
            log.warning("Could not start Infinite mode for user %s: %s", uid, exc)
            await self.send_text(
                interaction, self.t("infinite.generation_failed"),
                title=self.t("infinite.generation_failed_title"), ephemeral=True, colour=discord.Colour.red(),
            )

    async def skip_infinite(self, interaction, owner_id=None):
        """Reveal the current map without a guess, then change the button to Next random map."""
        uid = interaction.user.id
        if owner_id is not None and uid != owner_id:
            await self.send_text(
                interaction, self.t("infinite.session_owner_error"),
                title=self.t("infinite.session_title"), ephemeral=True, colour=discord.Colour.red(),
            )
            return

        session = self.infinite_sessions.get((interaction.guild_id, uid))
        if session is None:
            await self.send_text(
                interaction, self.t("infinite.start_first"),
                title=self.t("infinite.session_title"), ephemeral=True, colour=discord.Colour.orange(),
            )
            return
        if session.guessed:
            await self.start_infinite(interaction, owner_id=uid, edit_message=True)
            return

        # A skipped map breaks a consecutive-correct streak, but does not affect the best.
        session.guessed = True
        current_streak, best_streak = db.record_infinite_result(
            interaction.guild_id, uid, correct=False
        )
        streak_status = self.t(
            "infinite.streak_status", current=current_streak, best=best_streak
        )
        filename = f"{secrets.token_hex(6)}.jpg"
        embed = discord.Embed(
            title=self.t("infinite.skipped_title", map_name=session.game_map["name"]),
            description=self.t(
                "infinite.skipped_description", map_name=session.game_map["name"],
                next_button=self.t("infinite.button_next"),
            ),
            colour=discord.Colour.orange(),
        )
        embed.add_field(name=self.t("common.creator_field"), value=creators_text(session.game_map))
        embed.add_field(
            name=self.t("infinite.streak_field"), value=streak_status, inline=False,
        )
        embed.add_field(
            name=self.t("infinite.filters_field"),
            value="\n".join(f"• {label}" for label in session.effect_labels),
            inline=False,
        )
        if session.game_map.get("code"):
            embed.add_field(name=self.t("common.code_field"), value=session.game_map["code"])
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(text=self.t("infinite.footer"))
        file = discord.File(session.path, filename=filename)

        await interaction.response.defer()
        session.message = await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=InfiniteView(self, uid, completed=True, session=session),
        )

    async def handle_infinite_guess(self, interaction, text):
        key = (interaction.guild_id, interaction.user.id)
        session = self.infinite_sessions.get(key)
        if text == INFINITE_NO_SESSION_CHOICE or session is None:
            await self.send_text(
                interaction, self.t("infinite.start_first"),
                title=self.t("infinite.session_title"), ephemeral=True, colour=discord.Colour.orange(),
            )
            return
        if text == INFINITE_FINISHED_CHOICE or session.guessed:
            await self.send_text(
                interaction, self.t("infinite.already_complete", next_button=self.t("infinite.button_next")),
                title=self.t("infinite.already_complete_title"), ephemeral=True, colour=discord.Colour.orange(),
            )
            return

        answer = (text or "").strip()
        if not answer:
            await self.send_text(
                interaction, self.t("infinite.missing_guess"),
                title=self.t("infinite.missing_guess_title"),
            )
            return
        if len(answer) > config.MAX_GUESS_LENGTH:
            await self.send_text(
                interaction, self.t("infinite.guess_too_long", max_length=config.MAX_GUESS_LENGTH),
                title=self.t("infinite.guess_too_long_title"), colour=discord.Colour.red(),
            )
            return

        # Consume the one guess before sending any response, preventing concurrent submissions.
        session.guessed = True
        correct = is_match(answer, session.game_map)
        current_streak, best_streak = db.record_infinite_result(
            interaction.guild_id, interaction.user.id, correct=correct
        )
        streak_status = self.t(
            "infinite.streak_status", current=current_streak, best=best_streak
        )
        if correct:
            description = self.t("infinite.correct_description")
            title = self.t("infinite.correct_title", map_name=session.game_map["name"])
            colour = discord.Colour.green()
        else:
            description = self.t(
                "infinite.incorrect_description", map_name=session.game_map["name"]
            )
            title = self.t("infinite.incorrect_title")
            colour = discord.Colour.orange()

        filename = f"{secrets.token_hex(6)}.jpg"
        embed = discord.Embed(title=title, description=description, colour=colour)
        embed.add_field(name=self.t("common.creator_field"), value=creators_text(session.game_map))
        embed.add_field(
            name=self.t("infinite.streak_field"), value=streak_status, inline=False,
        )
        embed.add_field(
            name=self.t("infinite.filters_field"),
            value="\n".join(f"• {label}" for label in session.effect_labels), inline=False,
        )
        if session.game_map.get("code"):
            embed.add_field(name=self.t("common.code_field"), value=session.game_map["code"])
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(text=self.t("infinite.footer"))
        file = discord.File(session.path, filename=filename)
        await interaction.response.send_message(
            embed=embed,
            file=file,
            view=InfiniteView(self, interaction.user.id, completed=True, session=session),
            ephemeral=True,
        )
        # The result is now shown in a new ephemeral reply with the correct Next button.
        # Remove the old Skip button where Discord still permits editing that ephemeral message.
        if session.message is not None:
            try:
                await session.message.edit(view=None)
            except discord.HTTPException:
                log.debug("Could not remove the previous Infinite view for user %s", interaction.user.id)
        try:
            session.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    def resolve_player_challenge_mode(self, guild_id, user_id):
        """Infer the selected challenge mode for /guesschallenge when mode is omitted."""
        candidates = []
        difficulty = self.get_challenge(guild_id, "easy")
        if difficulty:
            selected = db.get_difficulty_selection(guild_id, difficulty.challenge_key, user_id)
            if selected in DIFFICULTY_MODES:
                candidates.append(selected)

        zoom = self.get_challenge(guild_id, "zoom")
        if zoom:
            used, solved = db.get_mode_attempt(guild_id, zoom.challenge_key, "zoom", user_id)
            timer_key = (guild_id, zoom.challenge_key, "zoom", user_id)
            if timer_key in self.mode_timers or used > 0 or solved:
                candidates.append("zoom")

        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            return None, (
                self.t("challenge.multiple_selected")
            )
        if difficulty:
            return None, self.t("challenge.select_difficulty_first")
        if zoom:
            return None, self.t("challenge.start_zoom_first", start_zoom_button=self.t("buttons.start_zoom_challenge"))
        return None, self.t("challenge.no_active_for_player")

    async def begin_challenge(self, interaction, mode):
        """Select a difficulty or start zoom-out, then privately reveal its image."""
        challenge = self.get_challenge(interaction.guild_id, mode)
        if not challenge:
            await self._say(interaction, self.t("challenge.no_active_challenge"))
            return

        uid = interaction.user.id
        previous_selection = None
        selection_changed = False
        if mode in DIFFICULTY_MODES:
            previous_selection, selected, selection_changed, selection_status = (
                db.select_or_downgrade_difficulty(
                    challenge.guild_id, challenge.challenge_key, uid, mode
                )
            )
            if selection_status == "harder":
                await self._say(
                    interaction,
                    self.t("challenge.harder_selection", selected=self.mode_title(previous_selection)),
                )
                return
            if selection_status == "already_attempted":
                await self._say(
                    interaction,
                    self.t("challenge.already_attempted_selection"),
                )
                return
            if selected != mode:
                await self._say(
                    interaction,
                    self.t("challenge.selected_different", selected=self.mode_title(selected)),
                )
                return

        used, solved = db.get_mode_attempt(
            challenge.guild_id, challenge.challenge_key, mode, uid
        )
        max_attempts = (
            getattr(config, "DIFFICULTY_CHALLENGE_MAX_ATTEMPTS", 1)
            if mode in DIFFICULTY_MODES else config.MAX_ATTEMPTS
        )
        if solved or used >= max_attempts:
            await self._say(
                interaction,
                self.t("challenge.difficulty_already_guessed")
                if mode in DIFFICULTY_MODES else self.t("challenge.zoom_all_tries_used"),
            )
            return

        timer_key = (challenge.guild_id, challenge.challenge_key, mode, uid)
        previous_timer_key = (
            (challenge.guild_id, challenge.challenge_key, previous_selection, uid)
            if previous_selection in DIFFICULTY_MODES and previous_selection != mode else None
        )
        if timer_key in self.mode_timers or timer_key in self.mode_starting:
            await self._say(
                interaction,
                self.t("challenge.challenge_timer_running"),
            )
            return

        self.mode_starting.add(timer_key)
        try:
            mode_text = self.mode_title(mode)
            if mode in DIFFICULTY_MODES:
                description = self.t(
                    "challenge.difficulty_start_description",
                    mode_label=mode_text, points=f"{self.challenge_points(mode):g}",
                )
            else:
                points = "/".join(str(value) for value in config.POINTS_BY_ATTEMPT)
                description = self.t(
                    "challenge.zoom_start_description", mode_label=mode_text,
                    attempts=config.MAX_ATTEMPTS, points=points,
                )
            await interaction.response.defer(ephemeral=True, thinking=True)
            image_attempt = used if mode == "zoom" else 0
            file = await images.mode_file(challenge.path, mode, image_attempt)
            if mode in DIFFICULTY_MODES and db.get_difficulty_selection(
                challenge.guild_id, challenge.challenge_key, uid
            ) != mode:
                await interaction.followup.send(
                    embed=self.text_embed(
                        self.t("challenge.selection_changed_during_start"),
                        title=self.t("common.challenge_updated_title"), colour=discord.Colour.orange(),
                    ),
                    ephemeral=True,
                )
                return
            embed = discord.Embed(
                title=self.t("challenge.image_title", mode_label=mode_text),
                description=description, colour=discord.Colour.blurple(),
            )
            embed.set_image(url=f"attachment://{file.filename}")
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
            if previous_timer_key is not None:
                self.mode_timers.pop(previous_timer_key, None)
            self.mode_timers[timer_key] = time.monotonic()
        except (discord.HTTPException, OSError, ValueError) as exc:
            log.warning("Could not start %s challenge for user %s: %s", mode, uid, exc)
            # If this interaction created the lock but failed before showing the
            # image, release it so the player is not permanently locked out.
            if selection_changed and mode in DIFFICULTY_MODES:
                db.restore_difficulty_selection(
                    challenge.guild_id, challenge.challenge_key, uid, mode, previous_selection
                )
            if interaction.response.is_done():
                await self.send_text(
                    interaction, self.t("challenge.image_unavailable"),
                    title=self.t("common.image_unavailable_title"), ephemeral=True, colour=discord.Colour.red(),
                )
            else:
                await self._say(interaction, self.t("challenge.image_unavailable"))
        finally:
            self.mode_starting.discard(timer_key)

    async def _show_mode_image(self, interaction, challenge, mode, attempt_index, description):
        await interaction.response.defer(ephemeral=True, thinking=True)
        file = await images.mode_file(challenge.path, mode, attempt_index)
        embed = discord.Embed(
            title=self.t("challenge.image_title", mode_label=self.mode_title(mode)),
            description=description, colour=discord.Colour.blurple(),
        )
        embed.set_image(url=f"attachment://{file.filename}")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        timer_key = (challenge.guild_id, challenge.challenge_key, mode, interaction.user.id)
        self.mode_timers[timer_key] = time.monotonic()

    async def _reveal_mode_answer(self, interaction, challenge, text):
        game_map = challenge.game_map
        filename = f"{secrets.token_hex(6)}.png"
        embed = discord.Embed(
            title=self.t("challenge.correct_title", map_name=game_map["name"]),
            description=text, colour=discord.Colour.gold(),
        )
        embed.add_field(name=self.t("common.creator_field"), value=creators_text(game_map))
        if game_map.get("code"):
            embed.add_field(name=self.t("common.code_field"), value=game_map["code"])
        embed.set_image(url=f"attachment://{filename}")
        file = discord.File(challenge.path, filename=filename)
        await interaction.response.send_message(embed=embed, file=file, ephemeral=True)

    async def handle_mode_guess(self, interaction, mode, text):
        """Handle a one-guess difficulty entry or a multi-guess zoom-out entry."""
        if text == MODE_START_FIRST_CHOICE:
            await self._say(interaction, self.t("challenge.start_first"))
            return
        if text == MODE_ALREADY_FINISHED_CHOICE:
            finished_mode = mode
            if finished_mode is None:
                finished_mode, _problem = self.resolve_player_challenge_mode(
                    interaction.guild_id, interaction.user.id
                )
            message = (
                self.t("challenge.zoom_all_tries_used")
                if finished_mode == "zoom"
                else self.t("challenge.difficulty_already_guessed")
            )
            await self._say(interaction, message)
            return
        if text == MODE_NO_CHALLENGE_CHOICE:
            if mode is None:
                _resolved, problem = self.resolve_player_challenge_mode(
                    interaction.guild_id, interaction.user.id
                )
                await self._say(interaction, problem or self.t("challenge.select_first"))
            else:
                await self._say(interaction, self.t("challenge.not_active_for_mode"))
            return

        if mode is None:
            mode, problem = self.resolve_player_challenge_mode(
                interaction.guild_id, interaction.user.id
            )
            if mode is None:
                await self._say(interaction, problem)
                return

        if mode not in CHALLENGE_MODES:
            await self._say(interaction, self.t("challenge.invalid_mode"))
            return

        challenge = self.get_challenge(interaction.guild_id, mode)
        if not challenge:
            await self._say(interaction, self.t("challenge.no_active_challenge"))
            return

        uid = interaction.user.id
        if mode in DIFFICULTY_MODES:
            selected = db.get_difficulty_selection(
                challenge.guild_id, challenge.challenge_key, uid
            )
            if selected is None:
                await self._say(
                    interaction,
                    self.t("challenge.select_button_before_guess"),
                )
                return
            if selected != mode:
                await self._say(
                    interaction,
                    self.t("challenge.harder_selection", selected=self.mode_title(selected)),
                )
                return

        used, solved = db.get_mode_attempt(challenge.guild_id, challenge.challenge_key, mode, uid)
        max_attempts = (
            getattr(config, "DIFFICULTY_CHALLENGE_MAX_ATTEMPTS", 1)
            if mode in DIFFICULTY_MODES else config.MAX_ATTEMPTS
        )
        if solved or used >= max_attempts:
            message = (
                self.t("challenge.difficulty_already_guessed")
                if mode in DIFFICULTY_MODES else self.t("challenge.zoom_all_tries_used")
            )
            await self._say(interaction, message)
            return

        answer = (text or "").strip()
        if not answer:
            await self._say(interaction, self.t("challenge.empty_guess"))
            return
        if len(answer) > config.MAX_GUESS_LENGTH:
            await self._say(interaction, self.t("challenge.guess_too_long", max_length=config.MAX_GUESS_LENGTH))
            return

        timer_key = (challenge.guild_id, challenge.challenge_key, mode, uid)
        started = self.mode_timers.get(timer_key)
        if started is None:
            await self._say(
                interaction,
                self.t("challenge.resume_after_restart"),
            )
            return

        correct = is_match(answer, challenge.game_map)
        used_now = used + 1
        if mode in DIFFICULTY_MODES:
            recorded = db.record_difficulty_attempt(
                challenge.guild_id, challenge.challenge_key, mode, uid, correct
            )
            if not recorded:
                self.mode_timers.pop(timer_key, None)
                selected_now = db.get_difficulty_selection(
                    challenge.guild_id, challenge.challenge_key, uid
                )
                await self._say(
                    interaction,
                    self.t("challenge.selected_now", selected=self.mode_title(selected_now)),
                )
                return
        else:
            db.set_mode_attempt(challenge.guild_id, challenge.challenge_key, mode, uid, used_now, correct)
        db.update_mode_map_result(
            challenge.guild_id, uid, mode, challenge.challenge_key,
            challenge.game_map["id"], used_now, correct,
        )
        elapsed = max(0.0, time.monotonic() - started)
        self.mode_timers.pop(timer_key, None)

        if correct:
            if mode in DIFFICULTY_MODES:
                points = self.challenge_points(mode)
                score_detail = self.t("challenge.fixed_reward_detail", points=f"{points:g}")
            else:
                base_points = config.POINTS_BY_ATTEMPT[used]
                bonus = self.speed_bonus(elapsed)
                points = round(base_points + bonus, 2)
                score_detail = self.t(
                    "challenge.speed_reward_detail",
                    base_points=f"{base_points:g}", bonus=f"{bonus:g}",
                )
            total = db.add_mode_points(challenge.guild_id, uid, mode, points)
            text_out = self.t(
                "challenge.correct_result", mode_label=self.mode_title(mode),
                points=f"{points:g}", score_detail=score_detail, mode=mode,
                total=f"{total:g}", elapsed=elapsed,
            )
            await self._reveal_mode_answer(interaction, challenge, text_out)
            return

        left = max_attempts - used_now
        if mode == "zoom" and left > 0:
            description = self.t("challenge.incorrect_zoom_retry", attempts_left=left)
            await self._show_mode_image(interaction, challenge, mode, used_now, description)
        else:
            miss_text = (
                self.t("challenge.incorrect_difficulty")
                if mode in DIFFICULTY_MODES
                else self.t("challenge.incorrect_zoom_final", attempts=max_attempts)
            )
            await self._reveal_mode_answer(interaction, challenge, miss_text)


    # --------------------------------------------------------- round logic
    async def pick_map(self, guild_id):
        """Pick and mark a map while serialized against other game modes in this guild."""
        lock = self.map_pick_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            limit = min(config.RECENT_MAPS_MEMORY, max(0, len(self.maps) - 1))
            recent = set(db.recent_ids(guild_id, limit))
            pool = [m for m in self.maps if m["id"] not in recent and m["id"] not in self.bad]
            if not pool:
                pool = [m for m in self.maps if m["id"] not in self.bad]
            if not pool:
                self.bad.clear()
                pool = list(self.maps)
            random.shuffle(pool)
            for m in pool[:10]:                       # a few tries in case an image is dead
                path = await images.ensure_image(self.session, m)
                if path:
                    db.add_recent(guild_id, m["id"])
                    return m, path
                self.bad.add(m["id"])
            return None

    async def start_round(self, channel, *, advance_schedule=False):
        """Serialize map starts in a channel to prevent duplicate concurrent rounds."""
        lock = self.round_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            error = await self._start_round_locked(channel)
            if not error and advance_schedule:
                db.schedule_next(channel.guild.id, next_reset())
            return error

    async def _start_round_locked(self, channel):
        """Post a new map in `channel`; caller must hold its per-channel lock."""
        picked = await self.pick_map(channel.guild.id)
        if not picked:
            return self.t("main.map_fetch_failed")
        game_map, path = picked
        old = self.rounds.get(channel.id)
        if old:
            await self.close_round(channel, old)

        points = "/".join(str(p) for p in config.POINTS_BY_ATTEMPT)
        text = self.t(
            "main.round_post_description",
            start_button=self.t("buttons.start_guessing"), guess_command=self.guess_mention,
            attempts=config.MAX_ATTEMPTS, points=points, bonus=f"{SPEED_BONUS_MAX:g}",
            bonus_interval=SPEED_BONUS_INTERVAL_SECONDS, bonus_cutoff=SPEED_BONUS_CUTOFF_SECONDS,
            next_reset=next_reset(),
        )
        try:
            message = await channel.send(
                embed=self.text_embed(text, title=self.t("main.round_post_title")),
                view=GuessView(self),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't post the map in %s: %s", channel.id, e)
            return self.t("main.map_post_failed")
        started_at = int(time.time() * 1000)
        self.rounds[channel.id] = Round(
            channel.guild.id, channel.id, game_map, path, message.id, started_at
        )
        db.save_round(channel.id, channel.guild.id, game_map["id"], message.id, started_at)
        return None

    async def retire_post(self, channel, rnd):
        """Remove the button and the now-stale 'Next map <countdown>' line from a finished map's post."""
        if not rnd.message_id:
            return
        try:
            message = await channel.fetch_message(rnd.message_id)
            if message.embeds:
                embed = message.embeds[0]
                lines = [
                    line for line in (embed.description or "").split("\n")
                    if not ("<t:" in line and ":R>" in line)
                ]
                embed.description = "\n".join(lines + [self.t("common.map_ended_line")])
            else:
                legacy_text = message.content or self.t("common.map_ended_line")
                embed = self.text_embed(
                    legacy_text + "\n" + self.t("common.map_ended_line"),
                    title=self.t("main.post_end_title"),
                )
            await message.edit(content=None, embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            pass                                      # message deleted or not editable: nothing to do

    async def close_round(self, channel, rnd):
        """End a map: remove its button and publicly reveal the answer."""
        self.rounds.pop(rnd.channel_id, None)
        db.delete_round(rnd.channel_id)
        await self.retire_post(channel, rnd)
        text = self.t("main.day_over", map_name=rnd.game_map["name"], solvers=len(rnd.solved))
        summary = db.map_round_summary(rnd.guild_id, rnd.channel_id, rnd.started_at)
        if summary["participants"]:
            average = (
                f"{summary['average_solve_time_ms'] / 1000:.1f}s"
                if summary["average_solve_time_ms"] is not None else self.t("main.recap_not_available")
            )
            text += "\n\n" + self.t(
                "main.daily_recap", participants=summary["participants"],
                solved=summary["solved"], unsolved=summary["participants"] - summary["solved"],
                first_try=summary["first_try"], second_try=summary["second_try"],
                third_try=summary["third_try"], average_solve_time=average,
            )
        else:
            text += "\n\n" + self.t("main.daily_recap_empty")
        extra_fields = []
        if summary["fastest_user_id"] is not None:
            extra_fields.append((
                self.t("main.fastest_solver_field"),
                self.t(
                    "main.fastest_solver_value",
                    user=f"<@{summary['fastest_user_id']}>",
                    solve_time=f"{summary['fastest_solve_time_ms'] / 1000:.1f}s",
                ),
            ))
        await self.reveal(channel, rnd, text, extra_fields=extra_fields)

    async def reveal(self, channel, rnd, text, interaction=None, ephemeral=False, extra_fields=None):
        """Show the answer: name, creator, code and the unblurred image.
        With an interaction (+ ephemeral=True) only that player sees it."""
        m = rnd.game_map
        fname = f"{secrets.token_hex(6)}.png"
        embed = discord.Embed(
            title=self.t("main.reveal_title", map_name=m["name"]),
            description=text, colour=discord.Colour.gold(),
        )
        embed.add_field(name=self.t("common.creator_field"), value=creators_text(m))
        if m.get("code"):
            embed.add_field(name=self.t("common.code_field"), value=m["code"])
        for field_name, field_value in (extra_fields or []):
            embed.add_field(name=str(field_name)[:256], value=str(field_value)[:1024], inline=False)
        embed.set_image(url=f"attachment://{fname}")
        try:
            file = discord.File(rnd.path, filename=fname)
            if interaction:
                await interaction.response.send_message(
                    embed=embed, file=file, ephemeral=ephemeral,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await channel.send(
                    embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none()
                )
        except discord.HTTPException as e:
            log.warning("Reveal failed in %s: %s", rnd.channel_id, e)

    # ------------------------------------------------------------ guessing
    async def _say(self, interaction, text):
        """A private (only-you-see-it) reply."""
        try:
            await self.send_text(interaction, text, title=self.t("common.default_embed_title"), ephemeral=True)
        except discord.HTTPException as e:
            log.warning("Reply failed: %s", e)

    async def _announce(self, channel, name, attempt, points):
        """Public 'X solved it!' message that doesn't reveal the answer."""
        if not config.ANNOUNCE_CORRECT or channel is None or not self.msgs.has("main.announce_correct"):
            return
        try:
            text = self.t("main.announce_correct", user=name, attempt=attempt, points=points)
            await channel.send(
                embed=self.text_embed(text, title=self.t("main.announce_title")),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            log.warning("Announcement failed: %s", e)

    async def _player_state(self, interaction):
        """Checks shared by /guess and /pass. Returns (round, tries used) if this player can still
        play; otherwise replies privately and returns None."""
        rnd = self.rounds.get(interaction.channel_id)
        if not rnd:
            await self._say(interaction, self.t("main.no_active_map"))
            return None
        uid = interaction.user.id
        if uid in rnd.solved:
            await self._say(interaction, self.t("main.already_solved_next", next_reset=next_reset()))
            return None
        used = rnd.used.get(uid, 0)
        if used >= config.MAX_ATTEMPTS:
            await self._say(interaction, self.t("main.all_tries_next", next_reset=next_reset()))
            return None
        if uid not in rnd.attempt_started_at:
            await self._say(
                interaction, self.t("main.start_first", start_button=self.t("buttons.start_guessing"))
            )
            return None
        return rnd, used

    async def _show_clearer(self, interaction, rnd, used):
        """Private reply with the next, less blurry image (`used` = tries this player has now used)."""
        left = config.MAX_ATTEMPTS - used
        msg = self.t("main.wrong_guess", user=interaction.user.display_name, attempts_left=left)
        msg += self.t("main.wrong_guess_retry", guess_command=self.guess_mention)
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = await self.map_message(msg, rnd.path, used)
        await interaction.followup.send(ephemeral=True, **payload)
        rnd.attempt_started_at[interaction.user.id] = time.monotonic()

    async def handle_pass(self, interaction):
        """/pass: spend a try without guessing, to see a clearer image."""
        state = await self._player_state(interaction)
        if not state:
            return
        rnd, used = state
        if used + 1 >= config.MAX_ATTEMPTS:
            await self._say(interaction, self.t("main.last_try_cannot_pass"))
            return
        uid = interaction.user.id
        rnd.used[uid] = used + 1                      # count it before any await (no races)
        db.set_attempt(rnd.channel_id, uid, used + 1, False)
        db.update_map_result(
            rnd.guild_id, uid, rnd.channel_id, rnd.started_at, rnd.game_map["id"],
            used + 1, False, started_playing_at=rnd.player_started_at.get(uid, 0),
        )
        try:
            await self._show_clearer(interaction, rnd, used + 1)
        except discord.HTTPException as e:
            log.warning("Pass response failed: %s", e)

    async def handle_guess(self, interaction, text):
        """Process a /guess submission. All feedback and map images are private."""
        if text in (ALREADY_FINISHED_CHOICE, NO_ACTIVE_MAP_CHOICE, START_FIRST_CHOICE):
            await self._player_state(interaction)
            return
        state = await self._player_state(interaction)
        if not state:
            return
        rnd, used = state
        text = (text or "").strip()
        if not text:
            await self._say(interaction, self.t("main.empty_guess"))
            return
        if len(text) > config.MAX_GUESS_LENGTH:
            await self._say(
                interaction, self.t("main.guess_too_long", max_length=config.MAX_GUESS_LENGTH)
            )
            return

        uid = interaction.user.id
        correct = is_match(text, rnd.game_map)
        rnd.used[uid] = used + 1                      # count it before any await (no races)
        if correct:
            rnd.solved.add(uid)
        db.set_attempt(rnd.channel_id, uid, used + 1, correct)
        started_playing_at = rnd.player_started_at.get(uid, 0)
        solve_time_ms = (
            max(1, int(time.time() * 1000) - started_playing_at)
            if correct and started_playing_at else 0
        )
        db.update_map_result(
            rnd.guild_id, uid, rnd.channel_id, rnd.started_at, rnd.game_map["id"],
            used + 1, correct, started_playing_at=started_playing_at,
            solve_time_ms=solve_time_ms,
        )
        name = interaction.user.display_name
        map_name = rnd.game_map["name"]

        try:
            if correct:
                base_points = config.POINTS_BY_ATTEMPT[used]
                elapsed = time.monotonic() - rnd.attempt_started_at.get(uid, time.monotonic())
                bonus = self.speed_bonus(elapsed)
                points = round(base_points + bonus, 2)
                total = db.add_points(rnd.guild_id, uid, points)
                key = CORRECT_KEYS[min(used, len(CORRECT_KEYS) - 1)]
                msg = self.t(
                    f"main.{key}", user=name, points=f"{points:g}",
                    total=f"{total:g}", map_name=map_name,
                )
                msg += self.t("main.speed_bonus_line", bonus=f"{bonus:g}", elapsed=elapsed)
                # the answer is shown ONLY to the person who guessed it
                await self.reveal(interaction.channel, rnd, msg, interaction=interaction, ephemeral=True)
                await self._announce(interaction.channel, name, used + 1, f"{points:g}")
                return

            left = config.MAX_ATTEMPTS - (used + 1)
            if left > 0:
                await self._show_clearer(interaction, rnd, used + 1)      # less blur each try
            else:
                # final miss: reveal the map, but only to this player
                msg = self.t("main.out_of_attempts", user=name, map_name=map_name)
                await self.reveal(interaction.channel, rnd, msg, interaction=interaction, ephemeral=True)
        except discord.HTTPException as e:
            log.warning("Guess response failed: %s", e)

    async def ensure_scheduled_challenge(self, guild_id, channel, kind, challenge_key, force=False):
        """Create a scheduled challenge once per key, or replace it for admin testing."""
        existing = db.get_scheduled_challenge(guild_id, kind)
        if not force and existing and existing[3] == channel.id and (
            existing[2] == challenge_key or existing[2].startswith(f"TEST-{challenge_key}-")
        ):
            cache_key = (guild_id, kind)
            if cache_key not in self.scheduled_challenges:
                by_id = {m["id"]: m for m in self.maps}
                game_map = by_id.get(existing[4])
                path = await images.ensure_image(self.session, game_map) if game_map else None
                if path:
                    self.scheduled_challenges[cache_key] = ScheduledChallenge(
                        guild_id, kind, existing[2], existing[3], game_map, path, existing[5]
                    )
            return False

        picked = await self.pick_map(guild_id)
        if not picked:
            log.warning("Couldn't select a map for %s challenge in guild %s", kind, guild_id)
            return False
        game_map, path = picked

        if kind == "difficulty":
            challenge_title = self.t("challenge.difficulty_announcement_title", challenge_key=challenge_key)
            content = self.t(
                "challenge.difficulty_announcement",
                challenge_key=challenge_key,
                rewards=self.challenge_rewards_text(),
                easy_label=self.t("modes.easy.label"),
                normal_label=self.t("modes.normal.label"),
                hard_label=self.t("modes.hard.label"),
                impossible_label=self.t("modes.impossible.label"),
            )
            view = DifficultyChallengeView(self)
        else:
            challenge_title = self.t("challenge.zoom_announcement_title", challenge_key=challenge_key)
            content = self.t(
                "challenge.zoom_announcement",
                challenge_key=challenge_key,
                attempts=config.MAX_ATTEMPTS,
                points="/".join(str(value) for value in config.POINTS_BY_ATTEMPT),
                speed_bonus=f"{SPEED_BONUS_MAX:g}",
                bonus_interval=SPEED_BONUS_INTERVAL_SECONDS,
                bonus_cutoff=SPEED_BONUS_CUTOFF_SECONDS,
            )
            view = ZoomChallengeView(self)

        try:
            message = await channel.send(
                embed=self.text_embed(content, title=challenge_title),
                view=view,
            )
        except discord.HTTPException as exc:
            log.warning("Couldn't post %s challenge in guild %s: %s", kind, guild_id, exc)
            return False

        db.save_scheduled_challenge(
            guild_id, kind, challenge_key, channel.id, game_map["id"], message.id
        )
        if existing and existing[5] and (force or existing[2] != challenge_key or existing[3] != channel.id):
            try:
                old_channel = self.get_channel(existing[3])
                if old_channel is None:
                    old_channel = await self.fetch_channel(existing[3])
                if isinstance(old_channel, discord.TextChannel):
                    old_message = await old_channel.fetch_message(existing[5])
                    if old_message.embeds:
                        old_embed = old_message.embeds[0]
                        old_embed.description = (old_embed.description or "") + self.t("challenge.replaced_line")
                    else:
                        old_embed = self.text_embed(
                            (old_message.content or "") + self.t("challenge.replaced_line"),
                            title=self.t("challenge.replaced_title"),
                        )
                    await old_message.edit(content=None, embed=old_embed, attachments=[], view=None)
            except discord.HTTPException:
                pass
        if existing and existing[2] != challenge_key:
            old_key = existing[2]
            self.mode_timers = {
                timer_key: started for timer_key, started in self.mode_timers.items()
                if not (timer_key[0] == guild_id and timer_key[1] == old_key)
            }
        self.scheduled_challenges[(guild_id, kind)] = ScheduledChallenge(
            guild_id, kind, challenge_key, channel.id, game_map, path, message.id
        )
        return True

    # ------------------------------------------------ daily challenge scheduler
    @tasks.loop(seconds=30)
    async def scheduled_challenge_loop(self):
        """Post one difficulty challenge each day and zoom-out challenges on configured weekdays."""
        try:
            now = datetime.now(timezone.utc)
            difficulty_hour = int(getattr(config, "DIFFICULTY_CHALLENGE_HOUR_UTC", 12))
            difficulty_minute = int(getattr(config, "DIFFICULTY_CHALLENGE_MINUTE_UTC", 0))
            zoom_hour = int(getattr(config, "ZOOM_CHALLENGE_HOUR_UTC", 12))
            zoom_minute = int(getattr(config, "ZOOM_CHALLENGE_MINUTE_UTC", 0))
            zoom_days = set(getattr(config, "ZOOM_CHALLENGE_WEEKDAYS_UTC", (0, 3)))
            day_key = now.date().isoformat()

            for guild_id, channel_id, _next_at in db.all_settings():
                channel = self.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.HTTPException:
                        log.warning("Could not find challenge channel %s", channel_id)
                        continue
                if not isinstance(channel, discord.TextChannel):
                    continue

                if (now.hour, now.minute) >= (difficulty_hour, difficulty_minute):
                    await self.ensure_scheduled_challenge(
                        guild_id, channel, "difficulty", day_key
                    )
                if now.weekday() in zoom_days and (now.hour, now.minute) >= (zoom_hour, zoom_minute):
                    await self.ensure_scheduled_challenge(guild_id, channel, "zoom", day_key)
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scheduled_challenge_loop crashed this tick")

    @scheduled_challenge_loop.before_loop
    async def _wait_for_challenges(self):
        await self.wait_until_ready()

    # ------------------------------------------------------ daily leaderboard
    @tasks.loop(seconds=15)
    async def daily_leaderboards(self):
        """Post the current top 10 once per UTC day at the configured time."""
        try:
            now = datetime.now(timezone.utc)
            target_hour = getattr(
                config, "DAILY_LEADERBOARD_HOUR_UTC", 23
            )
            target_minute = getattr(config, "DAILY_LEADERBOARD_MINUTE_UTC", 55)
            if (now.hour, now.minute) < (target_hour, target_minute):
                return

            day_key = now.date().isoformat()
            for guild_id, channel_id, _next_at in db.all_settings():
                if (db.has_daily_leaderboard_post(guild_id, day_key)
                        or self.leaderboard_posted.get(str(guild_id)) == day_key):
                    continue
                channel = self.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.HTTPException:
                        log.warning("Could not find leaderboard channel %s", channel_id)
                        continue
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    await channel.send(
                        embed=self.leaderboard_embed(guild_id),
                        view=PlayView(self),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException as e:
                    log.warning("Daily leaderboard post failed in guild %s: %s", guild_id, e)
                    continue
                db.mark_daily_leaderboard_post(guild_id, day_key)
                self.leaderboard_posted[str(guild_id)] = day_key
                self._save_leaderboard_state()
        except Exception:  # noqa: BLE001 - keep the background loop alive
            log.exception("daily_leaderboards crashed this tick")

    @daily_leaderboards.before_loop
    async def _wait_for_daily_leaderboard(self):
        await self.wait_until_ready()

    # ------------------------------------------------------ weekly leaderboard
    @tasks.loop(seconds=30)
    async def weekly_leaderboards(self):
        """Post the previous ISO week's standings once per week at the configured UTC time."""
        try:
            now = datetime.now(timezone.utc)
            target_day = int(getattr(config, "WEEKLY_LEADERBOARD_DAY_UTC", 0))
            target_hour = int(getattr(config, "WEEKLY_LEADERBOARD_HOUR_UTC", 0))
            target_minute = int(getattr(config, "WEEKLY_LEADERBOARD_MINUTE_UTC", 5))
            days_since_target = (now.weekday() - target_day) % 7
            target_date = now.date() - timedelta(days=days_since_target)
            target_at = datetime(
                target_date.year, target_date.month, target_date.day,
                target_hour, target_minute, tzinfo=timezone.utc,
            )
            if now < target_at:
                return
            # If the bot was offline at the scheduled minute, the next tick still
            # posts the most recently completed ISO week rather than missing it.
            week_key = db.previous_week_key(target_at)
            for guild_id, channel_id, _next_at in db.all_settings():
                if db.has_weekly_leaderboard_post(guild_id, week_key):
                    continue
                channel = self.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.HTTPException:
                        log.warning("Could not find weekly leaderboard channel %s", channel_id)
                        continue
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    await channel.send(
                        embed=self.weekly_leaderboard_embed(guild_id, week_key),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException as e:
                    log.warning("Weekly leaderboard post failed in guild %s: %s", guild_id, e)
                    continue
                db.mark_weekly_leaderboard_post(guild_id, week_key)
        except Exception:  # noqa: BLE001 - keep the scheduled loop alive
            log.exception("weekly_leaderboards crashed this tick")

    @weekly_leaderboards.before_loop
    async def _wait_for_weekly_leaderboard(self):
        await self.wait_until_ready()

    # ------------------------------------------------------ automatic maps
    @tasks.loop(seconds=5)
    async def auto_rounds(self):
        try:
            now = int(time.time())
            for guild_id, channel_id, _next_at in db.all_settings():
                channel = self.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.fetch_channel(channel_id)
                    except discord.HTTPException:
                        log.warning("Could not fetch configured map channel %s", channel_id)
                        continue
                if not isinstance(channel, discord.TextChannel):
                    continue

                lock = self.round_locks.setdefault(channel_id, asyncio.Lock())
                async with lock:
                    # Re-read after acquiring the lock. A manual /guessmap newmap may
                    # have advanced the schedule while this loop was waiting.
                    current_setting = next(
                        (row for row in db.all_settings() if row[0] == guild_id), None
                    )
                    if current_setting is None:
                        continue
                    next_at = current_setting[2]
                    due = next_at <= now
                    if not due and (channel_id in self.rounds or self.next_try.get(channel_id, 0) > now):
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
        except Exception:  # noqa: BLE001 - keep the background loop alive whatever happens
            log.exception("auto_rounds crashed this tick")

    @auto_rounds.before_loop
    async def _wait(self):
        await self.wait_until_ready()

    @tasks.loop(hours=config.MAPS_REFRESH_HOURS)
    async def refresh_maps(self):
        if self.refresh_maps.current_loop == 0:       # already loaded at startup
            return
        try:
            self.maps = await load_maps(self.session)
            self.bad.clear()
        except (OSError, ValueError):
            log.exception("Map refresh failed; keeping the current list")


class LeaderboardView(discord.ui.View):
    """Small pager for the full leaderboard, avoiding Discord embed size limits."""
    PAGE_SIZE = LEADERBOARD_PAGE_SIZE

    def __init__(self, bot, owner_id, guild_id, rows, mode=None, infinite_streaks=False,
                 weekly=False, week_key=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.rows = rows
        self.mode = mode
        self.infinite_streaks = infinite_streaks
        self.weekly = weekly
        self.week_key = week_key
        self.page = 0
        self.max_page = max(0, (len(rows) - 1) // self.PAGE_SIZE)
        self.previous_button.label = bot.t("buttons.previous_page")[:80]
        self.next_button.label = bot.t("buttons.next_page")[:80]
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    async def _change_page(self, interaction, delta):
        if interaction.user.id != self.owner_id:
            await self.bot.send_text(
                interaction, self.bot.t("common_responses.leaderboard_permission"),
                title=self.bot.t("common.leaderboard_controls_title"), ephemeral=True, colour=discord.Colour.red(),
            )
            return
        self.page = max(0, min(self.max_page, self.page + delta))
        self._update_buttons()
        if self.infinite_streaks:
            embed = self.bot.infinite_streak_leaderboard_embed(
                self.guild_id, rows=self.rows, page=self.page,
                page_size=self.PAGE_SIZE, full=True,
            )
        elif self.weekly:
            embed = self.bot.weekly_leaderboard_embed(
                self.guild_id, week_key=self.week_key, rows=self.rows, page=self.page,
                page_size=self.PAGE_SIZE, full=True,
            )
        else:
            embed = self.bot.leaderboard_embed(
                self.guild_id, rows=self.rows, page=self.page,
                page_size=self.PAGE_SIZE, full=True, mode=self.mode,
            )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="…", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="…", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, 1)


# ------------------------------------------------------------------ commands
def make_group(bot):
    group = app_commands.Group(name="guessmap", description=bot.t("commands.group_description"), guild_only=True)

    async def require_admin(interaction):
        if interaction.user.guild_permissions.manage_guild:
            return True
        await bot.send_text(
            interaction, bot.t("admin.permission_body"),
            title=bot.t("common.permission_title"), ephemeral=True, colour=discord.Colour.red(),
        )
        return False

    @group.command(name="setup", description=bot.t("commands.setup_description"))
    @app_commands.describe(channel=bot.t("commands.setup_channel_parameter"))
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction):
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.attach_files and perms.embed_links and perms.read_message_history):
            await bot.send_text(
                interaction,
                bot.t("main.permission_missing_body"),
                title=bot.t("main.permission_missing_title"), ephemeral=True, colour=discord.Colour.red(),
            )
            return
        fresh = channel.id not in bot.rounds
        interval_minutes = round(config.MAP_INTERVAL_HOURS * 60)
        db.set_schedule(
            interaction.guild_id, channel.id, interval_minutes,
            int(time.time()) if fresh else next_reset(),
        )
        first = bot.t("main.setup_first_within_minute") if fresh else bot.t("main.setup_current_until_next")
        hour = bot.t("common.utc_hour", hour=f"{config.DAILY_RESET_HOUR_UTC:02d}")
        minutes = round(config.MAP_INTERVAL_HOURS * 60)
        if minutes == 1440:
            every = bot.t("main.setup_every_day", hour=hour)
        elif minutes % 60 == 0:
            every = bot.t("main.setup_every_hours", hours=minutes // 60, hour=hour)
        else:
            every = bot.t("main.setup_every_minutes", minutes=minutes)
        await bot.send_text(
            interaction,
            bot.t(
                "main.setup_success", channel=channel.mention, frequency=every, first=first,
                leaderboard_hour=f"{getattr(config, 'DAILY_LEADERBOARD_HOUR_UTC', config.DAILY_RESET_HOUR_UTC):02d}",
                leaderboard_minute=f"{getattr(config, 'DAILY_LEADERBOARD_MINUTE_UTC', 0):02d}",
                weekly_day=calendar.day_name[int(getattr(config, 'WEEKLY_LEADERBOARD_DAY_UTC', 0))],
                weekly_hour=f"{int(getattr(config, 'WEEKLY_LEADERBOARD_HOUR_UTC', 0)):02d}",
                weekly_minute=f"{int(getattr(config, 'WEEKLY_LEADERBOARD_MINUTE_UTC', 5)):02d}",
            ),
            title=bot.t("main.setup_success_title"), ephemeral=True, colour=discord.Colour.green(),
        )

    @group.command(name="disable", description=bot.t("commands.disable_description"))
    async def disable(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        db.disable(interaction.guild_id)
        await bot.send_text(
            interaction, bot.t("main.daily_maps_disabled"), title=bot.t("main.daily_maps_disabled_title"),
            ephemeral=True, colour=discord.Colour.orange(),
        )

    @group.command(name="newmap", description=bot.t("commands.newmap_description"))
    async def newmap(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        err = await bot.start_round(interaction.channel, advance_schedule=True)
        await bot.send_text(
            interaction, err or bot.t("main.new_map_posted"),
            title=bot.t("main.map_start_error_title") if err else bot.t("main.map_post_success_title"),
            ephemeral=True, colour=discord.Colour.red() if err else discord.Colour.green(),
        )

    @group.command(name="play", description=bot.t("commands.play_description"))
    async def play(interaction: discord.Interaction):
        await bot.begin_guessing(interaction)

    @group.command(name="challenge", description=bot.t("commands.challenge_description"))
    async def challenge(interaction: discord.Interaction):
        active = bot.get_challenge(interaction.guild_id, "easy")
        if not active:
            await bot.send_text(
                interaction, bot.t("challenge.no_active_daily"),
                title=bot.t("challenge.no_active_title"), ephemeral=True, colour=discord.Colour.orange(),
            )
            return
        embed = bot.text_embed(
            bot.t("challenge.difficulty_private_description", rewards=bot.challenge_rewards_text()),
            title=bot.t("challenge.difficulty_private_title"),
        )
        await interaction.response.send_message(embed=embed, view=DifficultyChallengeView(bot), ephemeral=True)

    @group.command(name="infinite", description=bot.t("commands.infinite_description"))
    async def infinite(interaction: discord.Interaction):
        await bot.start_infinite(interaction)

    @group.command(name="testchallenge", description=bot.t("commands.testchallenge_description"))
    @app_commands.choices(kind=[
        app_commands.Choice(name=bot.t("commands.testchallenge_difficulty_choice"), value="difficulty"),
        app_commands.Choice(name=bot.t("commands.testchallenge_zoom_choice"), value="zoom"),
    ])
    @app_commands.describe(kind=bot.t("commands.testchallenge_kind_parameter"))
    async def testchallenge(interaction: discord.Interaction, kind: str = "difficulty"):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        day_key = datetime.now(timezone.utc).date().isoformat()
        challenge_key = f"TEST-{day_key}-{datetime.now(timezone.utc):%H%M%S}-{time.time_ns() % 1_000_000:06d}"
        created = await bot.ensure_scheduled_challenge(
            interaction.guild_id, interaction.channel, kind, challenge_key, force=True
        )
        await bot.send_text(
            interaction,
            bot.t("challenge.test_success") if created else bot.t("challenge.test_failure"),
            title=bot.t("challenge.test_title"), ephemeral=True,
            colour=discord.Colour.green() if created else discord.Colour.red(),
        )

    @group.command(name="leaderboard", description=bot.t("commands.leaderboard_description"))
    @app_commands.describe(full=bot.t("commands.leaderboard_full_parameter"))
    async def leaderboard(interaction: discord.Interaction, full: bool = False):
        rows = db.top(interaction.guild_id, None if full else 10)
        view = LeaderboardView(bot, interaction.user.id, interaction.guild_id, rows) if full and len(rows) > 20 else None
        send_kwargs = {
            "embed": bot.leaderboard_embed(interaction.guild_id, rows=rows, full=full),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            send_kwargs["view"] = view
        await interaction.response.send_message(**send_kwargs)

    @group.command(name="weeklyleaderboard", description=bot.t("commands.weeklyleaderboard_description"))
    @app_commands.describe(
        week=bot.t("commands.weeklyleaderboard_week_parameter"),
        full=bot.t("commands.weeklyleaderboard_full_parameter"),
    )
    async def weeklyleaderboard(
        interaction: discord.Interaction, week: str | None = None, full: bool = False
    ):
        if week is None or not week.strip():
            week_key = db.current_week_key()
        else:
            candidate = week.strip().upper()
            try:
                parsed = datetime.strptime(candidate + "-1", "%G-W%V-%u")
                week_key = db.current_week_key(parsed)
                if week_key != candidate:
                    raise ValueError("non-canonical ISO week")
            except ValueError:
                await bot.send_text(
                    interaction, bot.t("weekly.invalid_week", example=db.current_week_key()),
                    title=bot.t("weekly.invalid_week_title"), ephemeral=True,
                    colour=discord.Colour.orange(),
                )
                return
        rows = db.top_weekly(interaction.guild_id, week_key, None if full else 10)
        view = (
            LeaderboardView(
                bot, interaction.user.id, interaction.guild_id, rows, weekly=True, week_key=week_key
            )
            if full and len(rows) > LEADERBOARD_PAGE_SIZE else None
        )
        send_kwargs = {
            "embed": bot.weekly_leaderboard_embed(
                interaction.guild_id, week_key=week_key, rows=rows, full=full
            ),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            send_kwargs["view"] = view
        await interaction.response.send_message(**send_kwargs)

    @group.command(name="stats", description=bot.t("commands.stats_description"))
    @app_commands.describe(member=bot.t("commands.stats_member_parameter"))
    async def stats(interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        await interaction.response.send_message(
            embed=bot.player_profile_embed(interaction.guild_id, target),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="challengeleaderboard", description=bot.t("commands.challengeleaderboard_description"))
    @app_commands.choices(mode=[
        app_commands.Choice(name=bot.mode_title("easy"), value="easy"),
        app_commands.Choice(name=bot.mode_title("normal"), value="normal"),
        app_commands.Choice(name=bot.mode_title("hard"), value="hard"),
        app_commands.Choice(name=bot.mode_title("impossible"), value="impossible"),
        app_commands.Choice(name=bot.mode_title("zoom"), value="zoom"),
    ])
    @app_commands.describe(
        mode=bot.t("commands.challengeleaderboard_mode_parameter"),
        full=bot.t("commands.challengeleaderboard_full_parameter"),
    )
    async def challengeleaderboard(interaction: discord.Interaction, mode: str, full: bool = False):
        rows = db.top_mode(interaction.guild_id, mode, None if full else 10)
        view = LeaderboardView(bot, interaction.user.id, interaction.guild_id, rows, mode=mode) if full and len(rows) > 20 else None
        send_kwargs = {
            "embed": bot.leaderboard_embed(interaction.guild_id, rows=rows, full=full, mode=mode),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            send_kwargs["view"] = view
        await interaction.response.send_message(**send_kwargs)

    @group.command(name="infiniteleaderboard", description=bot.t("commands.infiniteleaderboard_description"))
    @app_commands.describe(full=bot.t("commands.infiniteleaderboard_full_parameter"))
    async def infiniteleaderboard(interaction: discord.Interaction, full: bool = False):
        rows = db.top_infinite_streaks(interaction.guild_id, None if full else 10)
        view = (
            LeaderboardView(
                bot, interaction.user.id, interaction.guild_id, rows, infinite_streaks=True
            )
            if full and len(rows) > LEADERBOARD_PAGE_SIZE else None
        )
        send_kwargs = {
            "embed": bot.infinite_streak_leaderboard_embed(
                interaction.guild_id, rows=rows, full=full,
            ),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            send_kwargs["view"] = view
        await interaction.response.send_message(**send_kwargs)

    @group.command(name="reload", description=bot.t("commands.reload_description"))
    async def reload(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)   # fetching can take longer than 3 seconds
        try:
            new_maps = await load_maps(bot.session)
            bot.msgs.load()
        except (OSError, ValueError) as e:
            await bot.send_text(
                interaction, bot.t("common_responses.reload_failed", error=e),
                title=bot.t("common_responses.reload_failed_title"), ephemeral=True, colour=discord.Colour.red(),
            )
            return
        bot.maps = new_maps
        bot.bad.clear()
        await bot.send_text(
            interaction, bot.t("common_responses.reload_success", map_count=len(bot.maps)),
            title=bot.t("common_responses.reload_success_title"), ephemeral=True, colour=discord.Colour.green(),
        )

    return group


def make_guess_command(bot):
    @app_commands.command(name="guess", description=bot.t("commands.guess_description"))
    @app_commands.describe(name=bot.t("commands.guess_name_parameter"))
    @app_commands.guild_only()
    async def guess(interaction: discord.Interaction, name: str):
        await bot.handle_guess(interaction, name)

    @guess.autocomplete("name")
    async def guess_autocomplete(interaction: discord.Interaction, current: str):
        rnd = bot.rounds.get(interaction.channel_id)
        if not rnd:
            return [app_commands.Choice(name=bot.t("autocomplete.no_active_map"), value=NO_ACTIVE_MAP_CHOICE)]
        uid = interaction.user.id
        if uid in rnd.solved:
            return [app_commands.Choice(name=bot.t("autocomplete.already_solved"), value=ALREADY_FINISHED_CHOICE)]
        if rnd.used.get(uid, 0) >= config.MAX_ATTEMPTS:
            return [app_commands.Choice(name=bot.t("autocomplete.all_tries_used"), value=ALREADY_FINISHED_CHOICE)]
        if uid not in rnd.attempt_started_at:
            return [app_commands.Choice(name=bot.t("autocomplete.start_first"), value=START_FIRST_CHOICE)]
        names = suggest(bot.suggestions(), current, config.AUTOCOMPLETE_MAX_RESULTS)
        return [app_commands.Choice(name=n, value=n) for n in names]

    return guess


def make_guesschallenge_command(bot):
    @app_commands.command(
        name="guesschallenge",
        description=bot.t("commands.guesschallenge_description"),
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name=bot.mode_title("easy"), value="easy"),
        app_commands.Choice(name=bot.mode_title("normal"), value="normal"),
        app_commands.Choice(name=bot.mode_title("hard"), value="hard"),
        app_commands.Choice(name=bot.mode_title("impossible"), value="impossible"),
        app_commands.Choice(name=bot.mode_title("zoom"), value="zoom"),
    ])
    @app_commands.describe(
        name=bot.t("commands.guess_name_parameter"),
        mode=bot.t("commands.guesschallenge_mode_parameter"),
    )
    @app_commands.guild_only()
    async def guesschallenge(
        interaction: discord.Interaction,
        name: str,
        mode: str | None = None,
    ):
        await bot.handle_mode_guess(interaction, mode, name)

    @guesschallenge.autocomplete("name")
    async def challenge_autocomplete(interaction: discord.Interaction, current: str):
        mode = getattr(interaction.namespace, "mode", None)
        uid = interaction.user.id
        if mode is None:
            mode, problem = bot.resolve_player_challenge_mode(interaction.guild_id, uid)
            if mode is None:
                marker = MODE_NO_CHALLENGE_CHOICE
                return [app_commands.Choice(name=problem[:100], value=marker)]

        if mode not in CHALLENGE_MODES:
            return [app_commands.Choice(name=bot.t("autocomplete.valid_mode"), value=MODE_NO_CHALLENGE_CHOICE)]
        challenge = bot.get_challenge(interaction.guild_id, mode)
        if not challenge:
            return [app_commands.Choice(name=bot.t("autocomplete.no_active_mode"), value=MODE_NO_CHALLENGE_CHOICE)]

        if mode in DIFFICULTY_MODES:
            selected = db.get_difficulty_selection(
                challenge.guild_id, challenge.challenge_key, uid
            )
            if selected is None:
                return [app_commands.Choice(
                    name=bot.t("autocomplete.choose_difficulty"),
                    value=MODE_NO_CHALLENGE_CHOICE,
                )]
            if selected != mode:
                return [app_commands.Choice(
                    name=bot.t("autocomplete.selected_difficulty", mode_label=bot.mode_title(selected)),
                    value=MODE_NO_CHALLENGE_CHOICE,
                )]

        used, solved = db.get_mode_attempt(
            challenge.guild_id, challenge.challenge_key, mode, uid
        )
        max_attempts = (
            getattr(config, "DIFFICULTY_CHALLENGE_MAX_ATTEMPTS", 1)
            if mode in DIFFICULTY_MODES else config.MAX_ATTEMPTS
        )
        if solved or used >= max_attempts:
            status = (
                bot.t("autocomplete.already_guessed_one")
                if mode in DIFFICULTY_MODES else bot.t("autocomplete.zoom_tries_used")
            )
            return [app_commands.Choice(name=status, value=MODE_ALREADY_FINISHED_CHOICE)]

        timer_key = (challenge.guild_id, challenge.challenge_key, mode, uid)
        if timer_key not in bot.mode_timers:
            return [app_commands.Choice(
                name=bot.t("autocomplete.start_challenge_first"),
                value=MODE_START_FIRST_CHOICE,
            )]
        names = suggest(bot.suggestions(), current, config.AUTOCOMPLETE_MAX_RESULTS)
        return [app_commands.Choice(name=value, value=value) for value in names]

    return guesschallenge


def make_guessinfinite_command(bot):
    @app_commands.command(
        name="guessinfinite",
        description=bot.t("commands.guessinfinite_description"),
    )
    @app_commands.describe(name=bot.t("commands.guess_name_parameter"))
    @app_commands.guild_only()
    async def guessinfinite(interaction: discord.Interaction, name: str):
        await bot.handle_infinite_guess(interaction, name)

    @guessinfinite.autocomplete("name")
    async def infinite_autocomplete(interaction: discord.Interaction, current: str):
        session = bot.infinite_sessions.get((interaction.guild_id, interaction.user.id))
        if session is None:
            return [app_commands.Choice(
                name=bot.t("infinite.autocomplete_no_session"),
                value=INFINITE_NO_SESSION_CHOICE,
            )]
        if session.guessed:
            return [app_commands.Choice(
                name=bot.t("infinite.autocomplete_finished", next_button=bot.t("infinite.button_next")),
                value=INFINITE_FINISHED_CHOICE,
            )]
        names = suggest(bot.suggestions(), current, config.AUTOCOMPLETE_MAX_RESULTS)
        return [app_commands.Choice(name=value, value=value) for value in names]

    return guessinfinite


def make_pass_command(bot):
    @app_commands.command(name="pass", description=bot.t("commands.pass_description"))
    @app_commands.guild_only()
    async def pass_(interaction: discord.Interaction):
        await bot.handle_pass(interaction)

    return pass_


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable (see README).")
    GuessBot().run(token, log_handler=None)