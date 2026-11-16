import os
import sqlite3
import time
from datetime import datetime, timezone

import config

_conn = None


def init():
    global _conn
    os.makedirs(os.path.dirname(config.DB_FILE) or ".", exist_ok=True)
    _conn = sqlite3.connect(config.DB_FILE)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scores(
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            points REAL NOT NULL DEFAULT 0, correct INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id));

        CREATE TABLE IF NOT EXISTS settings(
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            interval_minutes INTEGER NOT NULL, next_round_at INTEGER NOT NULL);

        CREATE TABLE IF NOT EXISTS rounds(
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            map_id TEXT NOT NULL, message_id INTEGER, started_at INTEGER NOT NULL);

        CREATE TABLE IF NOT EXISTS attempts(
            channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0, solved INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(channel_id, user_id));

        CREATE TABLE IF NOT EXISTS recent(
            guild_id INTEGER NOT NULL, map_id TEXT NOT NULL, played_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS recent_idx ON recent(guild_id, played_at);

        -- Separate recency history for Infinite mode; daily picks are unaffected.
        CREATE TABLE IF NOT EXISTS infinite_recent(
            guild_id INTEGER NOT NULL, map_id TEXT NOT NULL, played_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS infinite_recent_idx
            ON infinite_recent(guild_id, played_at);

        CREATE TABLE IF NOT EXISTS player_map_stats(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            round_started_at INTEGER NOT NULL,
            map_id TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            solved INTEGER NOT NULL DEFAULT 0,
            solved_guess_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id, channel_id, round_started_at));
        CREATE INDEX IF NOT EXISTS player_map_stats_rank_idx
            ON player_map_stats(guild_id, user_id);

        -- Separate cumulative scoreboards for each challenge mode.
        CREATE TABLE IF NOT EXISTS mode_scores(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            points REAL NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id, mode));

        -- A player's progress on each scheduled challenge and mode.
        CREATE TABLE IF NOT EXISTS mode_attempts(
            guild_id INTEGER NOT NULL,
            challenge_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            solved INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, challenge_key, mode, user_id));

        -- One difficulty selection per player for each daily difficulty challenge.
        -- Selecting Easy, Normal, Hard or Impossible locks the other three.
        CREATE TABLE IF NOT EXISTS difficulty_selections(
            guild_id INTEGER NOT NULL,
            challenge_key TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            selected_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, challenge_key, user_id));

        -- Stats are one row per player per challenge and mode.
        CREATE TABLE IF NOT EXISTS mode_map_stats(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            challenge_key TEXT NOT NULL,
            map_id TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            solved INTEGER NOT NULL DEFAULT 0,
            solved_guess_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id, mode, challenge_key));
        CREATE INDEX IF NOT EXISTS mode_map_stats_rank_idx
            ON mode_map_stats(guild_id, mode, user_id);

        -- Retained history of maps used by the main daily game. Unlike `rounds`,
        -- this is never deleted when a round ends, so old UTC dates can be replayed.
        CREATE TABLE IF NOT EXISTS main_map_history(
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            map_id TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            PRIMARY KEY(channel_id, started_at));
        CREATE INDEX IF NOT EXISTS main_map_history_date_idx
            ON main_map_history(guild_id, started_at DESC);

        -- Persist the currently displayed challenge attempt so players can
        -- reopen its image after their ephemeral message disappears or the bot restarts.
        CREATE TABLE IF NOT EXISTS mode_sessions(
            guild_id INTEGER NOT NULL,
            challenge_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            attempt_started_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, challenge_key, mode, user_id));

        -- One active scheduled challenge per guild and challenge family.
        CREATE TABLE IF NOT EXISTS scheduled_challenges(
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            challenge_key TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            map_id TEXT NOT NULL,
            message_id INTEGER,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, kind));

        CREATE TABLE IF NOT EXISTS daily_leaderboard_posts(
            guild_id INTEGER NOT NULL,
            day_key TEXT NOT NULL,
            posted_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, day_key));

        -- Weekly main-game standings. ISO-week keys create a natural weekly reset
        -- while preserving past weeks for an archive.
        CREATE TABLE IF NOT EXISTS weekly_scores(
            guild_id INTEGER NOT NULL,
            week_key TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            points REAL NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, week_key, user_id));
        CREATE INDEX IF NOT EXISTS weekly_scores_rank_idx
            ON weekly_scores(guild_id, week_key, points DESC, correct DESC, user_id);

        CREATE TABLE IF NOT EXISTS weekly_leaderboard_posts(
            guild_id INTEGER NOT NULL,
            week_key TEXT NOT NULL,
            posted_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, week_key));

        -- Infinite mode has no points; this single row per player stores streak state.
        CREATE TABLE IF NOT EXISTS infinite_streaks(
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            current_streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id));
        CREATE INDEX IF NOT EXISTS infinite_streaks_rank_idx
            ON infinite_streaks(guild_id, best_streak DESC, current_streak DESC, user_id);
        """
    )
    # Older releases created these two tables without their intended keys.
    # CREATE TABLE IF NOT EXISTS does not repair an existing table, so rebuild
    # them before any ON CONFLICT(channel_id...) statements can run.
    _ensure_rounds_primary_key()
    _ensure_attempts_primary_key()

    # Upgrade databases created by the previous leaderboard update.
    columns = {row[1] for row in _conn.execute("PRAGMA table_info(player_map_stats)")}
    if "solved_guess_count" not in columns:
        _conn.execute(
            "ALTER TABLE player_map_stats ADD COLUMN solved_guess_count INTEGER NOT NULL DEFAULT 0"
        )
    # Earlier versions already stored the number of attempts on solved maps.
    # Backfill that history so the average-attempts statistic is not reset.
    _conn.execute(
        "UPDATE player_map_stats SET solved_guess_count=attempts "
        "WHERE solved=1 AND solved_guess_count=0 AND attempts>0"
    )
    columns = {row[1] for row in _conn.execute("PRAGMA table_info(player_map_stats)")}
    if "started_playing_at" not in columns:
        _conn.execute(
            "ALTER TABLE player_map_stats ADD COLUMN started_playing_at INTEGER NOT NULL DEFAULT 0"
        )
    if "solve_time_ms" not in columns:
        _conn.execute(
            "ALTER TABLE player_map_stats ADD COLUMN solve_time_ms INTEGER NOT NULL DEFAULT 0"
        )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS player_map_stats_round_idx "
        "ON player_map_stats(guild_id,channel_id,round_started_at,attempts,solved)"
    )

    # Backfill replay history from records that earlier versions kept. This
    # includes maps with player statistics and any round that is still active.
    # Older releases stored round timestamps in seconds; current releases use ms.
    _conn.execute(
        "INSERT OR IGNORE INTO main_map_history(guild_id,channel_id,map_id,started_at) "
        "SELECT DISTINCT guild_id,channel_id,map_id, "
        "CASE WHEN round_started_at < 100000000000 THEN round_started_at*1000 "
        "ELSE round_started_at END "
        "FROM player_map_stats WHERE round_started_at>0"
    )
    _conn.execute(
        "INSERT OR IGNORE INTO main_map_history(guild_id,channel_id,map_id,started_at) "
        "SELECT guild_id,channel_id,map_id, "
        "CASE WHEN started_at < 100000000000 THEN started_at*1000 ELSE started_at END "
        "FROM rounds WHERE started_at>0"
    )

    # Preserve an existing player's difficulty choice when upgrading from the
    # previous multi-attempt implementation. If they completed more than one
    # difficulty before this change, the first recorded mode is retained.
    _conn.execute(
        "INSERT OR IGNORE INTO difficulty_selections "
        "(guild_id,challenge_key,user_id,mode,selected_at) "
        "SELECT guild_id,challenge_key,user_id,mode,0 FROM mode_attempts "
        "WHERE mode IN ('easy','normal','hard','impossible') AND used>0 "
        "ORDER BY rowid ASC"
    )
    _conn.commit()


def _primary_key_columns(table):
    rows = _conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def _ensure_rounds_primary_key():
    """Repair legacy rounds tables, preserving the newest row per channel."""
    if _primary_key_columns("rounds") == ["channel_id"]:
        return

    columns = {row[1] for row in _conn.execute("PRAGMA table_info(rounds)")}
    required = {"channel_id", "guild_id", "map_id"}
    if not required.issubset(columns):
        raise sqlite3.DatabaseError(
            "The rounds table has an unsupported schema; back up scores.db before upgrading."
        )

    started_expr = "started_at" if "started_at" in columns else "0"
    message_expr = "message_id" if "message_id" in columns else "NULL"
    rows = _conn.execute(
        f"SELECT rowid, channel_id, guild_id, map_id, {message_expr}, {started_expr} "
        "FROM rounds ORDER BY rowid DESC"
    ).fetchall()

    # Keep the latest round for a channel (started_at is in milliseconds in
    # current versions; rowid breaks ties and supports older second timestamps).
    latest = {}
    for rowid, channel_id, guild_id, map_id, message_id, started_at in rows:
        current = latest.get(channel_id)
        candidate_key = (int(started_at or 0), int(rowid))
        current_key = (int(current[5] or 0), int(current[0])) if current else None
        if current is None or candidate_key > current_key:
            latest[channel_id] = (rowid, channel_id, guild_id, map_id, message_id, started_at or 0)

    with _conn:
        _conn.execute("DROP TABLE IF EXISTS rounds__key_migration")
        _conn.execute(
            "CREATE TABLE rounds__key_migration("
            "channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, "
            "map_id TEXT NOT NULL, message_id INTEGER, started_at INTEGER NOT NULL)"
        )
        _conn.executemany(
            "INSERT INTO rounds__key_migration "
            "(channel_id,guild_id,map_id,message_id,started_at) VALUES(?,?,?,?,?)",
            [row[1:] for row in latest.values()],
        )
        _conn.execute("DROP TABLE rounds")
        _conn.execute("ALTER TABLE rounds__key_migration RENAME TO rounds")


def _ensure_attempts_primary_key():
    """Repair legacy attempts tables, keeping the most recent row per player/map."""
    if _primary_key_columns("attempts") == ["channel_id", "user_id"]:
        return

    columns = {row[1] for row in _conn.execute("PRAGMA table_info(attempts)")}
    required = {"channel_id", "user_id"}
    if not required.issubset(columns):
        raise sqlite3.DatabaseError(
            "The attempts table has an unsupported schema; back up scores.db before upgrading."
        )
    used_expr = "used" if "used" in columns else "0"
    solved_expr = "solved" if "solved" in columns else "0"
    rows = _conn.execute(
        f"SELECT rowid, channel_id, user_id, {used_expr}, {solved_expr} "
        "FROM attempts ORDER BY rowid DESC"
    ).fetchall()

    latest = {}
    for _rowid, channel_id, user_id, used, solved in rows:
        latest.setdefault((channel_id, user_id), (channel_id, user_id, used or 0, solved or 0))

    with _conn:
        _conn.execute("DROP TABLE IF EXISTS attempts__key_migration")
        _conn.execute(
            "CREATE TABLE attempts__key_migration("
            "channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "used INTEGER NOT NULL DEFAULT 0, solved INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(channel_id,user_id))"
        )
        _conn.executemany(
            "INSERT INTO attempts__key_migration(channel_id,user_id,used,solved) VALUES(?,?,?,?)",
            list(latest.values()),
        )
        _conn.execute("DROP TABLE attempts")
        _conn.execute("ALTER TABLE attempts__key_migration RENAME TO attempts")


# ---- main daily scores --------------------------------------------------
def current_week_key(when=None):
    """Return the ISO week key (Monday-Sunday, UTC), e.g. 2026-W46."""
    if when is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(when, datetime):
        moment = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.fromtimestamp(float(when), tz=timezone.utc)
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def previous_week_key(when=None):
    """Return the previous ISO week relative to a UTC datetime/timestamp."""
    if when is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(when, datetime):
        moment = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.fromtimestamp(float(when), tz=timezone.utc)
    return current_week_key(moment.timestamp() - 7 * 86400)


def add_points(guild_id, user_id, points, when=None):
    """Update lifetime and current-week scores atomically."""
    week_key = current_week_key(when)
    with _conn:
        _conn.execute(
            "INSERT INTO scores(guild_id,user_id,points,correct) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET "
            "points=points+excluded.points, correct=correct+1",
            (guild_id, user_id, points),
        )
        _conn.execute(
            "INSERT INTO weekly_scores(guild_id,week_key,user_id,points,correct) "
            "VALUES(?,?,?,?,1) "
            "ON CONFLICT(guild_id,week_key,user_id) DO UPDATE SET "
            "points=weekly_scores.points+excluded.points, "
            "correct=weekly_scores.correct+1",
            (guild_id, week_key, user_id, points),
        )
    return _conn.execute(
        "SELECT points FROM scores WHERE guild_id=? AND user_id=?", (guild_id, user_id)
    ).fetchone()[0]


def top_weekly(guild_id, week_key=None, limit=10):
    week_key = week_key or current_week_key()
    sql = (
        "SELECT user_id,points,correct FROM weekly_scores "
        "WHERE guild_id=? AND week_key=? "
        "ORDER BY points DESC,correct DESC,user_id ASC"
    )
    params = [guild_id, week_key]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    return _conn.execute(sql, params).fetchall()


def _ranked_rows(score_table, guild_id, limit=None, mode=None):
    """Shared leaderboard query. Table names are internal constants, never user input."""
    if score_table == "scores":
        where = "s.guild_id=?"
        params = [guild_id]
        stats_where = "guild_id=?"
        stats_params = [guild_id]
        stats_table = "player_map_stats"
        join_mode = ""
    else:
        where = "s.guild_id=? AND s.mode=?"
        params = [guild_id, mode]
        stats_where = "guild_id=? AND mode=?"
        stats_params = [guild_id, mode]
        stats_table = "mode_map_stats"

    stats_mode_column = ", mode" if score_table == "mode_scores" else ""
    stats_group = ", mode" if score_table == "mode_scores" else ""
    stats_join = " AND p.mode=s.mode" if score_table == "mode_scores" else ""
    sql = (
        "SELECT s.user_id, s.points, s.correct, "
        "COALESCE(p.played,0), COALESCE(p.solved,0), p.avg_guesses "
        f"FROM {score_table} AS s "
        "LEFT JOIN ("
        f" SELECT user_id{stats_mode_column}, COUNT(*) AS played, SUM(solved) AS solved, "
        f" AVG(CASE WHEN solved=1 AND solved_guess_count>0 THEN solved_guess_count END) AS avg_guesses "
        f" FROM {stats_table} WHERE {stats_where} AND attempts>0 GROUP BY user_id{stats_group}"
        ") AS p ON p.user_id=s.user_id" + stats_join +
        f" WHERE {where} ORDER BY s.points DESC, s.correct DESC, s.user_id ASC"
    )
    all_params = stats_params + params
    if limit is not None:
        sql += " LIMIT ?"
        all_params.append(max(0, int(limit)))
    return _conn.execute(sql, all_params).fetchall()


def top(guild_id, limit=10):
    return _ranked_rows("scores", guild_id, limit)


def start_map_participation(guild_id, user_id, channel_id, round_started_at, map_id, started_playing_at):
    """Persist a player's first private reveal time without counting a played map yet."""
    with _conn:
        _conn.execute(
            "INSERT INTO player_map_stats "
            "(guild_id,user_id,channel_id,round_started_at,map_id,attempts,solved,solved_guess_count,"
            "started_playing_at,solve_time_ms) VALUES(?,?,?,?,?,0,0,0,?,0) "
            "ON CONFLICT(guild_id,user_id,channel_id,round_started_at) DO UPDATE SET "
            "started_playing_at=CASE WHEN player_map_stats.started_playing_at=0 "
            "THEN excluded.started_playing_at ELSE player_map_stats.started_playing_at END",
            (guild_id, user_id, channel_id, round_started_at, map_id, started_playing_at),
        )
    row = _conn.execute(
        "SELECT started_playing_at FROM player_map_stats "
        "WHERE guild_id=? AND user_id=? AND channel_id=? AND round_started_at=?",
        (guild_id, user_id, channel_id, round_started_at),
    ).fetchone()
    return int(row[0] or started_playing_at)


def load_map_start_times(channel_id, round_started_at):
    rows = _conn.execute(
        "SELECT user_id,started_playing_at FROM player_map_stats "
        "WHERE channel_id=? AND round_started_at=? AND started_playing_at>0",
        (channel_id, round_started_at),
    ).fetchall()
    return {int(user_id): int(started_at) for user_id, started_at in rows}


def update_map_result(guild_id, user_id, channel_id, round_started_at, map_id, attempts, solved,
                      started_playing_at=0, solve_time_ms=0):
    with _conn:
        _conn.execute(
            "INSERT INTO player_map_stats "
            "(guild_id,user_id,channel_id,round_started_at,map_id,attempts,solved,solved_guess_count,"
            "started_playing_at,solve_time_ms) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id,channel_id,round_started_at) DO UPDATE SET "
            "attempts=MAX(player_map_stats.attempts,excluded.attempts), "
            "solved=MAX(player_map_stats.solved,excluded.solved), "
            "solved_guess_count=CASE WHEN excluded.solved=1 THEN "
            "MIN(CASE WHEN player_map_stats.solved_guess_count=0 THEN excluded.solved_guess_count "
            "ELSE player_map_stats.solved_guess_count END, excluded.solved_guess_count) "
            "ELSE player_map_stats.solved_guess_count END, "
            "started_playing_at=CASE WHEN player_map_stats.started_playing_at=0 "
            "THEN excluded.started_playing_at ELSE player_map_stats.started_playing_at END, "
            "solve_time_ms=CASE WHEN excluded.solved=1 AND excluded.solve_time_ms>0 THEN "
            "CASE WHEN player_map_stats.solve_time_ms=0 THEN excluded.solve_time_ms "
            "ELSE MIN(player_map_stats.solve_time_ms,excluded.solve_time_ms) END "
            "ELSE player_map_stats.solve_time_ms END",
            (guild_id, user_id, channel_id, round_started_at, map_id, attempts,
             int(solved), attempts if solved else 0, int(started_playing_at or 0), int(solve_time_ms or 0)),
        )


def map_round_summary(guild_id, channel_id, round_started_at):
    """Aggregate recap stats for one exact daily-map instance."""
    row = _conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(solved),0), "
        "SUM(CASE WHEN solved=1 AND solved_guess_count=1 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN solved=1 AND solved_guess_count=2 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN solved=1 AND solved_guess_count>=3 THEN 1 ELSE 0 END), "
        "AVG(CASE WHEN solved=1 AND solve_time_ms>0 THEN solve_time_ms END) "
        "FROM player_map_stats WHERE guild_id=? AND channel_id=? AND round_started_at=? AND attempts>0",
        (guild_id, channel_id, round_started_at),
    ).fetchone()
    fastest = _conn.execute(
        "SELECT user_id,solve_time_ms FROM player_map_stats "
        "WHERE guild_id=? AND channel_id=? AND round_started_at=? "
        "AND solved=1 AND solve_time_ms>0 "
        "ORDER BY solve_time_ms ASC,user_id ASC LIMIT 1",
        (guild_id, channel_id, round_started_at),
    ).fetchone()
    return {
        "participants": int(row[0] or 0),
        "solved": int(row[1] or 0),
        "first_try": int(row[2] or 0),
        "second_try": int(row[3] or 0),
        "third_try": int(row[4] or 0),
        "average_solve_time_ms": float(row[5]) if row[5] is not None else None,
        "fastest_user_id": int(fastest[0]) if fastest else None,
        "fastest_solve_time_ms": int(fastest[1]) if fastest else None,
    }


def player_profile(guild_id, user_id, week_key=None):
    """Collect the compact set of lifetime, weekly, challenge and Infinite stats."""
    week_key = week_key or current_week_key()
    score = _conn.execute(
        "SELECT points,correct FROM scores WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone() or (0.0, 0)
    main_stats = _conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(solved),0), "
        "AVG(CASE WHEN solved=1 AND solved_guess_count>0 THEN solved_guess_count END), "
        "AVG(CASE WHEN solved=1 AND solve_time_ms>0 THEN solve_time_ms END) "
        "FROM player_map_stats WHERE guild_id=? AND user_id=? AND attempts>0",
        (guild_id, user_id),
    ).fetchone()
    weekly = _conn.execute(
        "SELECT points,correct FROM weekly_scores WHERE guild_id=? AND week_key=? AND user_id=?",
        (guild_id, week_key, user_id),
    ).fetchone() or (0.0, 0)
    mode_scores = _conn.execute(
        "SELECT mode,points,correct FROM mode_scores WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchall()
    mode_stats = _conn.execute(
        "SELECT mode,COUNT(*),COALESCE(SUM(solved),0) FROM mode_map_stats "
        "WHERE guild_id=? AND user_id=? AND attempts>0 GROUP BY mode",
        (guild_id, user_id),
    ).fetchall()
    streak = _conn.execute(
        "SELECT current_streak,best_streak FROM infinite_streaks "
        "WHERE guild_id=? AND user_id=?", (guild_id, user_id),
    ).fetchone() or (0, 0)
    return {
        "points": float(score[0] or 0), "correct": int(score[1] or 0),
        "played": int(main_stats[0] or 0), "solved": int(main_stats[1] or 0),
        "average_guesses": float(main_stats[2]) if main_stats[2] is not None else None,
        "average_solve_time_ms": float(main_stats[3]) if main_stats[3] is not None else None,
        "weekly_points": float(weekly[0] or 0), "weekly_correct": int(weekly[1] or 0),
        "mode_scores": {mode: {"points": float(points), "correct": int(correct)}
                        for mode, points, correct in mode_scores},
        "mode_stats": {mode: {"played": int(played), "solved": int(solved)}
                       for mode, played, solved in mode_stats},
        "infinite_current": int(streak[0]), "infinite_best": int(streak[1]),
        "week_key": week_key,
    }


# ---- challenge mode scores ---------------------------------------------
def add_mode_points(guild_id, user_id, mode, points):
    with _conn:
        _conn.execute(
            "INSERT INTO mode_scores(guild_id,user_id,mode,points,correct) VALUES(?,?,?,?,1) "
            "ON CONFLICT(guild_id,user_id,mode) DO UPDATE SET "
            "points=points+excluded.points, correct=correct+1",
            (guild_id, user_id, mode, points),
        )
    return _conn.execute(
        "SELECT points FROM mode_scores WHERE guild_id=? AND user_id=? AND mode=?",
        (guild_id, user_id, mode),
    ).fetchone()[0]


def top_mode(guild_id, mode, limit=10):
    return _ranked_rows("mode_scores", guild_id, limit, mode=mode)


def top_challenges(guild_id, limit=10):
    """Return one leaderboard aggregated across all challenge modes.

    Per-mode scores remain stored separately; this query combines their points,
    correct answers, participation counts and solved-map attempt averages.
    """
    sql = (
        "SELECT s.user_id,s.points,s.correct,COALESCE(p.played,0),"
        "COALESCE(p.solved,0),p.avg_guesses "
        "FROM ("
        " SELECT user_id,SUM(points) AS points,SUM(correct) AS correct "
        " FROM mode_scores WHERE guild_id=? GROUP BY user_id"
        ") AS s "
        "LEFT JOIN ("
        " SELECT user_id,COUNT(*) AS played,SUM(solved) AS solved,"
        " AVG(CASE WHEN solved=1 AND solved_guess_count>0 "
        "THEN solved_guess_count END) AS avg_guesses "
        " FROM mode_map_stats WHERE guild_id=? AND attempts>0 GROUP BY user_id"
        ") AS p ON p.user_id=s.user_id "
        "ORDER BY s.points DESC,s.correct DESC,s.user_id ASC"
    )
    params = [guild_id, guild_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    return _conn.execute(sql, params).fetchall()


def get_mode_attempt(guild_id, challenge_key, mode, user_id):
    row = _conn.execute(
        "SELECT used, solved FROM mode_attempts "
        "WHERE guild_id=? AND challenge_key=? AND mode=? AND user_id=?",
        (guild_id, challenge_key, mode, user_id),
    ).fetchone()
    return row if row else (0, 0)


def ensure_mode_session(guild_id, challenge_key, mode, user_id, now_ms=None):
    """Create a challenge session if needed and return its timestamps in UTC ms."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    with _conn:
        _conn.execute(
            "INSERT OR IGNORE INTO mode_sessions "
            "(guild_id,challenge_key,mode,user_id,started_at,attempt_started_at) "
            "VALUES(?,?,?,?,?,?)",
            (guild_id, challenge_key, mode, user_id, now_ms, now_ms),
        )
    return get_mode_session(guild_id, challenge_key, mode, user_id)


def get_mode_session(guild_id, challenge_key, mode, user_id):
    return _conn.execute(
        "SELECT started_at,attempt_started_at FROM mode_sessions "
        "WHERE guild_id=? AND challenge_key=? AND mode=? AND user_id=?",
        (guild_id, challenge_key, mode, user_id),
    ).fetchone()


def restart_mode_session_attempt(guild_id, challenge_key, mode, user_id, now_ms=None):
    """Record the start of a newly revealed attempt without changing session age."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    with _conn:
        _conn.execute(
            "INSERT INTO mode_sessions "
            "(guild_id,challenge_key,mode,user_id,started_at,attempt_started_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,challenge_key,mode,user_id) "
            "DO UPDATE SET attempt_started_at=excluded.attempt_started_at",
            (guild_id, challenge_key, mode, user_id, now_ms, now_ms),
        )
    return get_mode_session(guild_id, challenge_key, mode, user_id)


def delete_mode_session(guild_id, challenge_key, mode, user_id):
    with _conn:
        _conn.execute(
            "DELETE FROM mode_sessions WHERE guild_id=? AND challenge_key=? AND mode=? AND user_id=?",
            (guild_id, challenge_key, mode, user_id),
        )


def set_mode_attempt(guild_id, challenge_key, mode, user_id, used, solved):
    with _conn:
        _conn.execute(
            "INSERT INTO mode_attempts(guild_id,challenge_key,mode,user_id,used,solved) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,challenge_key,mode,user_id) "
            "DO UPDATE SET used=excluded.used, solved=excluded.solved",
            (guild_id, challenge_key, mode, user_id, used, int(solved)),
        )


def record_difficulty_attempt(guild_id, challenge_key, mode, user_id, solved):
    """Atomically record the one allowed daily-difficulty guess for the selected mode.

    Returns False if the player downgraded concurrently or already spent the guess.
    """
    if mode not in ("easy", "normal", "hard", "impossible"):
        raise ValueError(f"Not a difficulty mode: {mode}")
    with _conn:
        selected = _conn.execute(
            "SELECT mode FROM difficulty_selections "
            "WHERE guild_id=? AND challenge_key=? AND user_id=?",
            (guild_id, challenge_key, user_id),
        ).fetchone()
        if not selected or selected[0] != mode:
            return False
        previous_attempt = _conn.execute(
            "SELECT 1 FROM mode_attempts WHERE guild_id=? AND challenge_key=? "
            "AND user_id=? AND mode IN ('easy','normal','hard','impossible') "
            "AND (used>0 OR solved=1) LIMIT 1",
            (guild_id, challenge_key, user_id),
        ).fetchone()
        if previous_attempt:
            return False
        _conn.execute(
            "INSERT INTO mode_attempts(guild_id,challenge_key,mode,user_id,used,solved) "
            "VALUES(?,?,?,?,1,?) ON CONFLICT(guild_id,challenge_key,mode,user_id) "
            "DO UPDATE SET used=1, solved=excluded.solved",
            (guild_id, challenge_key, mode, user_id, int(solved)),
        )
    return True


def get_difficulty_selection(guild_id, challenge_key, user_id):
    row = _conn.execute(
        "SELECT mode FROM difficulty_selections "
        "WHERE guild_id=? AND challenge_key=? AND user_id=?",
        (guild_id, challenge_key, user_id),
    ).fetchone()
    return row[0] if row else None


def claim_difficulty_selection(guild_id, challenge_key, user_id, mode):
    """Atomically claim a daily difficulty choice. Return (chosen_mode, inserted)."""
    if mode not in ("easy", "normal", "hard", "impossible"):
        raise ValueError(f"Not a difficulty mode: {mode}")
    with _conn:
        cursor = _conn.execute(
            "INSERT OR IGNORE INTO difficulty_selections "
            "(guild_id,challenge_key,user_id,mode,selected_at) VALUES(?,?,?,?,?)",
            (guild_id, challenge_key, user_id, mode, int(time.time())),
        )
        row = _conn.execute(
            "SELECT mode FROM difficulty_selections "
            "WHERE guild_id=? AND challenge_key=? AND user_id=?",
            (guild_id, challenge_key, user_id),
        ).fetchone()
    return (row[0] if row else None), bool(cursor.rowcount)


def select_or_downgrade_difficulty(guild_id, challenge_key, user_id, mode):
    """Select a difficulty, or move to an easier one before the player has guessed.

    Returns (previous_mode, selected_mode, changed, status), where status is one of
    'selected', 'same', 'downgraded', 'harder', or 'already_attempted'.
    The transaction makes simultaneous button clicks safe.
    """
    order = {"easy": 0, "normal": 1, "hard": 2, "impossible": 3}
    if mode not in order:
        raise ValueError(f"Not a difficulty mode: {mode}")

    with _conn:
        row = _conn.execute(
            "SELECT mode FROM difficulty_selections "
            "WHERE guild_id=? AND challenge_key=? AND user_id=?",
            (guild_id, challenge_key, user_id),
        ).fetchone()

        if row is None:
            _conn.execute(
                "INSERT INTO difficulty_selections "
                "(guild_id,challenge_key,user_id,mode,selected_at) VALUES(?,?,?,?,?)",
                (guild_id, challenge_key, user_id, mode, int(time.time())),
            )
            return None, mode, True, "selected"

        previous = row[0]
        if previous == mode:
            return previous, previous, False, "same"

        # Higher index means harder. Upgrades are never allowed.
        if previous not in order or order[mode] > order[previous]:
            return previous, previous, False, "harder"

        attempt = _conn.execute(
            "SELECT 1 FROM mode_attempts "
            "WHERE guild_id=? AND challenge_key=? AND user_id=? "
            "AND mode IN ('easy','normal','hard','impossible') "
            "AND (used>0 OR solved=1) LIMIT 1",
            (guild_id, challenge_key, user_id),
        ).fetchone()
        if attempt:
            return previous, previous, False, "already_attempted"

        _conn.execute(
            "UPDATE difficulty_selections SET mode=?, selected_at=? "
            "WHERE guild_id=? AND challenge_key=? AND user_id=?",
            (mode, int(time.time()), guild_id, challenge_key, user_id),
        )
        return previous, mode, True, "downgraded"


def restore_difficulty_selection(guild_id, challenge_key, user_id, expected_mode, previous_mode):
    """Roll back a fresh selection/downgrade if its challenge image could not be shown."""
    with _conn:
        if previous_mode is None:
            _conn.execute(
                "DELETE FROM difficulty_selections WHERE guild_id=? AND challenge_key=? "
                "AND user_id=? AND mode=? AND NOT EXISTS ("
                "SELECT 1 FROM mode_attempts a WHERE a.guild_id=? AND a.challenge_key=? "
                "AND a.user_id=? AND a.used>0)",
                (guild_id, challenge_key, user_id, expected_mode,
                 guild_id, challenge_key, user_id),
            )
        else:
            _conn.execute(
                "UPDATE difficulty_selections SET mode=?, selected_at=? "
                "WHERE guild_id=? AND challenge_key=? AND user_id=? AND mode=? "
                "AND NOT EXISTS (SELECT 1 FROM mode_attempts a WHERE a.guild_id=? "
                "AND a.challenge_key=? AND a.user_id=? AND a.used>0)",
                (previous_mode, int(time.time()), guild_id, challenge_key, user_id,
                 expected_mode, guild_id, challenge_key, user_id),
            )


def release_difficulty_selection(guild_id, challenge_key, user_id, mode):
    """Undo a fresh selection only if no guess has been recorded for it."""
    with _conn:
        _conn.execute(
            "DELETE FROM difficulty_selections "
            "WHERE guild_id=? AND challenge_key=? AND user_id=? AND mode=? "
            "AND NOT EXISTS (SELECT 1 FROM mode_attempts a "
            "WHERE a.guild_id=? AND a.challenge_key=? AND a.mode=? AND a.user_id=? AND a.used>0)",
            (guild_id, challenge_key, user_id, mode,
             guild_id, challenge_key, mode, user_id),
        )


def update_mode_map_result(guild_id, user_id, mode, challenge_key, map_id, attempts, solved):
    with _conn:
        _conn.execute(
            "INSERT INTO mode_map_stats "
            "(guild_id,user_id,mode,challenge_key,map_id,attempts,solved,solved_guess_count) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id,mode,challenge_key) DO UPDATE SET "
            "attempts=MAX(mode_map_stats.attempts,excluded.attempts), "
            "solved=MAX(mode_map_stats.solved,excluded.solved), "
            "solved_guess_count=CASE WHEN excluded.solved=1 THEN excluded.solved_guess_count "
            "ELSE mode_map_stats.solved_guess_count END",
            (guild_id, user_id, mode, challenge_key, map_id, attempts,
             int(solved), attempts if solved else 0),
        )


# ---- scheduled difficulty / zoom-out challenges -----------------------
def save_scheduled_challenge(guild_id, kind, challenge_key, channel_id, map_id, message_id=None):
    with _conn:
        _conn.execute(
            "INSERT INTO scheduled_challenges "
            "(guild_id,kind,challenge_key,channel_id,map_id,message_id,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(guild_id,kind) DO UPDATE SET "
            "challenge_key=excluded.challenge_key, channel_id=excluded.channel_id, "
            "map_id=excluded.map_id, message_id=excluded.message_id, created_at=excluded.created_at",
            (guild_id, kind, challenge_key, channel_id, map_id, message_id, int(time.time())),
        )


def set_scheduled_challenge_message(guild_id, kind, message_id):
    with _conn:
        _conn.execute(
            "UPDATE scheduled_challenges SET message_id=? WHERE guild_id=? AND kind=?",
            (message_id, guild_id, kind),
        )


def get_scheduled_challenge(guild_id, kind):
    return _conn.execute(
        "SELECT guild_id,kind,challenge_key,channel_id,map_id,message_id,created_at "
        "FROM scheduled_challenges WHERE guild_id=? AND kind=?",
        (guild_id, kind),
    ).fetchone()


def all_scheduled_challenges():
    return _conn.execute(
        "SELECT guild_id,kind,challenge_key,channel_id,map_id,message_id,created_at "
        "FROM scheduled_challenges"
    ).fetchall()


def delete_scheduled_challenge(guild_id, kind):
    with _conn:
        _conn.execute(
            "DELETE FROM scheduled_challenges WHERE guild_id=? AND kind=?", (guild_id, kind)
        )


# ---- Infinite mode streaks -----------------------------------------------
def get_infinite_streak(guild_id, user_id):
    """Return (current_streak, best_streak), or (0, 0) for a new player."""
    row = _conn.execute(
        "SELECT current_streak,best_streak FROM infinite_streaks "
        "WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def record_infinite_result(guild_id, user_id, correct):
    """Record one resolved Infinite map atomically and return updated streak stats.

    Correct answers increment the current streak and raise the best when needed.
    Incorrect answers and skips reset only the current streak. No score or points
    table is touched. A single indexed row is updated per resolved map.
    """
    won = int(bool(correct))
    now = int(time.time())
    with _conn:
        _conn.execute(
            "INSERT INTO infinite_streaks "
            "(guild_id,user_id,current_streak,best_streak,updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET "
            "current_streak=CASE WHEN excluded.current_streak=1 "
            "THEN infinite_streaks.current_streak+1 ELSE 0 END, "
            "best_streak=MAX(infinite_streaks.best_streak, "
            "CASE WHEN excluded.current_streak=1 "
            "THEN infinite_streaks.current_streak+1 ELSE 0 END), "
            "updated_at=excluded.updated_at",
            (guild_id, user_id, won, won, now),
        )
        row = _conn.execute(
            "SELECT current_streak,best_streak FROM infinite_streaks "
            "WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
    return int(row[0]), int(row[1])


def top_infinite_streaks(guild_id, limit=10):
    """Return players with at least one correct answer, ordered by best streak."""
    sql = (
        "SELECT user_id,current_streak,best_streak FROM infinite_streaks "
        "WHERE guild_id=? AND best_streak>0 "
        "ORDER BY best_streak DESC,current_streak DESC,user_id ASC"
    )
    params = [guild_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    return _conn.execute(sql, params).fetchall()


# ---- daily leaderboard post tracking ----------------------------------
def has_daily_leaderboard_post(guild_id, day_key):
    return _conn.execute(
        "SELECT 1 FROM daily_leaderboard_posts WHERE guild_id=? AND day_key=?",
        (guild_id, day_key),
    ).fetchone() is not None


def mark_daily_leaderboard_post(guild_id, day_key):
    with _conn:
        _conn.execute(
            "INSERT OR IGNORE INTO daily_leaderboard_posts(guild_id,day_key,posted_at) VALUES(?,?,?)",
            (guild_id, day_key, int(time.time())),
        )


# ---- weekly leaderboard post tracking ----------------------------------
def has_weekly_leaderboard_post(guild_id, week_key):
    return _conn.execute(
        "SELECT 1 FROM weekly_leaderboard_posts WHERE guild_id=? AND week_key=?",
        (guild_id, week_key),
    ).fetchone() is not None


def mark_weekly_leaderboard_post(guild_id, week_key):
    with _conn:
        _conn.execute(
            "INSERT OR IGNORE INTO weekly_leaderboard_posts(guild_id,week_key,posted_at) "
            "VALUES(?,?,?)",
            (guild_id, week_key, int(time.time())),
        )


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
    return _conn.execute("SELECT guild_id,channel_id,next_round_at FROM settings").fetchall()


def schedule_next(guild_id, next_round_at):
    with _conn:
        _conn.execute("UPDATE settings SET next_round_at=? WHERE guild_id=?", (next_round_at, guild_id))


# ---- current daily map --------------------------------------------------
def save_round(channel_id, guild_id, map_id, message_id, started_at):
    with _conn:
        _conn.execute(
            "INSERT INTO rounds(channel_id,guild_id,map_id,message_id,started_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET guild_id=excluded.guild_id, map_id=excluded.map_id, "
            "message_id=excluded.message_id, started_at=excluded.started_at",
            (channel_id, guild_id, map_id, message_id, started_at),
        )
        _conn.execute(
            "INSERT OR IGNORE INTO main_map_history(guild_id,channel_id,map_id,started_at) "
            "VALUES(?,?,?,?)",
            (guild_id, channel_id, map_id, started_at),
        )
        _conn.execute("DELETE FROM attempts WHERE channel_id=?", (channel_id,))


def main_maps_on_date(guild_id, start_ms, end_ms):
    """Return main-game maps posted during one UTC date, newest first."""
    return _conn.execute(
        "SELECT channel_id,map_id,started_at FROM main_map_history "
        "WHERE guild_id=? AND started_at>=? AND started_at<? "
        "ORDER BY started_at DESC,channel_id ASC",
        (guild_id, int(start_ms), int(end_ms)),
    ).fetchall()


def delete_round(channel_id):
    with _conn:
        _conn.execute("DELETE FROM rounds WHERE channel_id=?", (channel_id,))
        _conn.execute("DELETE FROM attempts WHERE channel_id=?", (channel_id,))


def load_rounds():
    return _conn.execute(
        "SELECT channel_id,guild_id,map_id,message_id,started_at FROM rounds"
    ).fetchall()


def load_attempts(channel_id):
    return _conn.execute(
        "SELECT user_id,used,solved FROM attempts WHERE channel_id=?", (channel_id,)
    ).fetchall()


def set_attempt(channel_id, user_id, used, solved):
    with _conn:
        _conn.execute(
            "INSERT INTO attempts(channel_id,user_id,used,solved) VALUES(?,?,?,?) "
            "ON CONFLICT(channel_id,user_id) DO UPDATE SET used=excluded.used,solved=excluded.solved",
            (channel_id, user_id, used, int(solved)),
        )


# ---- recently played maps ----------------------------------------------
def add_recent(guild_id, map_id):
    with _conn:
        _conn.execute(
            "INSERT INTO recent(guild_id,map_id,played_at) VALUES(?,?,?)",
            (guild_id, map_id, int(time.time())),
        )


def recent_ids(guild_id, limit):
    rows = _conn.execute(
        "SELECT map_id FROM recent WHERE guild_id=? ORDER BY played_at DESC,rowid DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [row[0] for row in rows]



def add_infinite_recent(guild_id, map_id):
    """Record a map selected for Infinite mode, separately from daily history."""
    with _conn:
        _conn.execute(
            "INSERT INTO infinite_recent(guild_id,map_id,played_at) VALUES(?,?,?)",
            (guild_id, map_id, int(time.time())),
        )


def infinite_recent_ids(guild_id, limit):
    """Return the last N Infinite map selections for one server."""
    rows = _conn.execute(
        "SELECT map_id FROM infinite_recent WHERE guild_id=? "
        "ORDER BY played_at DESC,rowid DESC LIMIT ?",
        (guild_id, max(0, int(limit))),
    ).fetchall()
    return [row[0] for row in rows]
