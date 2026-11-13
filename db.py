import os
import sqlite3

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
        CREATE TABLE IF NOT EXISTS settings(
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            interval_minutes INTEGER NOT NULL, next_round_at INTEGER NOT NULL);
        """
    )
    _conn.commit()


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


def due_settings(now):
    return _conn.execute(
        "SELECT guild_id, channel_id, interval_minutes FROM settings WHERE next_round_at<=?", (now,)
    ).fetchall()


def schedule_next(guild_id, next_round_at):
    with _conn:
        _conn.execute("UPDATE settings SET next_round_at=? WHERE guild_id=?", (next_round_at, guild_id))
