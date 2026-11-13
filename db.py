import os
import sqlite3
import time

import config

_conn = None


def init():
    global _conn
    os.makedirs(os.path.dirname(config.DB_FILE) or ".", exist_ok=True)
    _conn = sqlite3.connect(config.DB_FILE)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scores(
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0, correct INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id));
        -- interval_minutes is no longer used (maps are daily) but the column is kept
        -- so an existing scores.db keeps working.
        CREATE TABLE IF NOT EXISTS settings(
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            interval_minutes INTEGER NOT NULL, next_round_at INTEGER NOT NULL);
        -- the map currently being played in each game channel (survives restarts)
        CREATE TABLE IF NOT EXISTS rounds(
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            map_id TEXT NOT NULL, message_id INTEGER, started_at INTEGER NOT NULL);
        -- each player's progress on the current map
        CREATE TABLE IF NOT EXISTS attempts(
            channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0, solved INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(channel_id, user_id));
        -- recently played maps, so they don't repeat soon (also across restarts)
        CREATE TABLE IF NOT EXISTS recent(
            guild_id INTEGER NOT NULL, map_id TEXT NOT NULL, played_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS recent_idx ON recent(guild_id, played_at);
        """
    )
    _conn.commit()


# ---- scores -------------------------------------------------------------
def add_points(guild_id, user_id, points):
    with _conn:
        _conn.execute(
            "INSERT INTO scores(guild_id,user_id,points,correct) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET points=points+excluded.points, correct=correct+1",
            (guild_id, user_id, points),
        )
    return _conn.execute(
        "SELECT points FROM scores WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ).fetchone()[0]


def top(guild_id, limit=10):
    return _conn.execute(
        "SELECT user_id, points, correct FROM scores WHERE guild_id=? "
        "ORDER BY points DESC, correct DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()


# ---- schedule -----------------------------------------------------------
def set_schedule(guild_id, channel_id, interval_minutes, next_round_at):
    with _conn:
        _conn.execute(
            "INSERT INTO settings(guild_id,channel_id,interval_minutes,next_round_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, "
            "interval_minutes=excluded.interval_minutes, next_round_at=excluded.next_round_at",
            (guild_id, channel_id, interval_minutes, next_round_at),
        )


def disable(guild_id):
    with _conn:
        _conn.execute("DELETE FROM settings WHERE guild_id=?", (guild_id,))


def all_settings():
    return _conn.execute("SELECT guild_id, channel_id, next_round_at FROM settings").fetchall()


def schedule_next(guild_id, next_round_at):
    with _conn:
        _conn.execute("UPDATE settings SET next_round_at=? WHERE guild_id=?", (next_round_at, guild_id))


# ---- the current map in a channel ----------------------------------------
def save_round(channel_id, guild_id, map_id, message_id, started_at):
    """Store a new current map for the channel and wipe everyone's attempts."""
    with _conn:
        _conn.execute(
            "INSERT INTO rounds(channel_id,guild_id,map_id,message_id,started_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET guild_id=excluded.guild_id, map_id=excluded.map_id, "
            "message_id=excluded.message_id, started_at=excluded.started_at",
            (channel_id, guild_id, map_id, message_id, started_at),
        )
        _conn.execute("DELETE FROM attempts WHERE channel_id=?", (channel_id,))


def delete_round(channel_id):
    with _conn:
        _conn.execute("DELETE FROM rounds WHERE channel_id=?", (channel_id,))
        _conn.execute("DELETE FROM attempts WHERE channel_id=?", (channel_id,))


def load_rounds():
    return _conn.execute("SELECT channel_id, guild_id, map_id, message_id FROM rounds").fetchall()


def load_attempts(channel_id):
    return _conn.execute(
        "SELECT user_id, used, solved FROM attempts WHERE channel_id=?", (channel_id,)
    ).fetchall()


def set_attempt(channel_id, user_id, used, solved):
    with _conn:
        _conn.execute(
            "INSERT INTO attempts(channel_id,user_id,used,solved) VALUES(?,?,?,?) "
            "ON CONFLICT(channel_id,user_id) DO UPDATE SET used=excluded.used, solved=excluded.solved",
            (channel_id, user_id, used, int(solved)),
        )


# ---- recently played maps -------------------------------------------------
def add_recent(guild_id, map_id):
    with _conn:
        _conn.execute(
            "INSERT INTO recent(guild_id,map_id,played_at) VALUES(?,?,?)",
            (guild_id, map_id, int(time.time())),
        )


def recent_ids(guild_id, limit):
    rows = _conn.execute(
        "SELECT map_id FROM recent WHERE guild_id=? ORDER BY played_at DESC, rowid DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [r[0] for r in rows]