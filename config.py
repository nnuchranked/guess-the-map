# --- Game rules ---------------------------------------------------------
MAX_ATTEMPTS = 3                      # tries per player, per round
POINTS_BY_ATTEMPT = [3, 2, 1]         # base points for a correct guess on try 1 / 2 / 3
BLUR_LEVELS = [0.07, 0.05, 0.03]     # blur radius as a fraction of reference width, per try
BLUR_MULTIPLIER = 1.0                 # scales all blur levels at once
BLUR_REFERENCE_WIDTH = 800
MAP_INTERVAL_HOURS = 12               # hours between main daily maps
DAILY_RESET_HOUR_UTC = 0              # schedule anchor hour (UTC)
ANNOUNCE_CORRECT = True               # announce correct solvers without revealing the answer
ONLY_TOURNAMENT_LEGAL = True          # only use maps with tournament_legal == true
EXCLUDE_UNDER_REVIEW = True           # skip maps flagged under_review
RECENT_MAPS_MEMORY = 50               # avoid repeating recently used maps across game modes

# --- Guess handling -----------------------------------------------------
FUZZY_THRESHOLD = 0.85                # 1.0 = exact only; lower = more typo-tolerant
MAX_GUESS_LENGTH = 256                # allows long map + creator combinations
AUTOCOMPLETE_MIN_CHARS = 2            # start suggestions after this many typed characters
AUTOCOMPLETE_MAX_RESULTS = 10         # Discord's maximum is 25

# --- Images / files -----------------------------------------------------
MAX_IMAGE_WIDTH = 800                 # cached images are downscaled to this width
MESSAGES_FILE = "messages.json"
CACHE_DIR = "cache"
DB_FILE = "data/scores.db"

# --- Main daily leaderboard ---------------------------------------------
# Posts once per UTC day, five minutes before the UTC day changes.
DAILY_LEADERBOARD_HOUR_UTC = 23
DAILY_LEADERBOARD_MINUTE_UTC = 55
DAILY_LEADERBOARD_STATE_FILE = "daily_leaderboard_state.json"
LEADERBOARD_PAGE_SIZE = 20

# --- Speed scoring ------------------------------------------------------
# The bonus starts at +0.5 and drops by 0.1 every 6 seconds, reaching zero at 30s.
SPEED_BONUS_MAX = 0.5
SPEED_BONUS_INTERVAL_SECONDS = 6
SPEED_BONUS_STEP = 0.1
SPEED_BONUS_CUTOFF_SECONDS = 30

# --- Daily difficulty challenge ----------------------------------------
# One map per day; each difficulty has its own attempts and leaderboard.
DIFFICULTY_CHALLENGE_HOUR_UTC = 12
DIFFICULTY_CHALLENGE_MINUTE_UTC = 0
DIFFICULTY_CHALLENGE_MAX_ATTEMPTS = 1
DIFFICULTY_CHALLENGE_POINTS = {
    "easy": 1,
    "normal": 2,
    "hard": 3,
    "impossible": 5,
}
IMPOSSIBLE_ZOOM_FACTOR = 2.5

# --- Zoom-out challenge -------------------------------------------------
# Twice weekly by default: Monday and Thursday (Monday=0 ... Sunday=6).
# For a weekly Wednesday challenge, change this to (2,).
ZOOM_CHALLENGE_WEEKDAYS_UTC = (0, 3)
ZOOM_CHALLENGE_HOUR_UTC = 12
ZOOM_CHALLENGE_MINUTE_UTC = 0
ZOOM_OUT_FACTORS = (4.0, 2.5, 1.0)   # starts zoomed in; final attempt shows the full map

# --- Challenge image styles --------------------------------------------
PIXELATION_WIDTHS = (28, 56, 112)    # each attempt reveals more detail

# --- Map source ---------------------------------------------------------
MAPS_URL = "https://compuch.com/api/levels"
MAPS_REFRESH_HOURS = 6
MAPS_FALLBACK_FILE = "data/maps_cache.json"

# --- Configuration validation ------------------------------------------
assert len(POINTS_BY_ATTEMPT) == MAX_ATTEMPTS, "POINTS_BY_ATTEMPT needs one entry per attempt"
assert len(BLUR_LEVELS) == MAX_ATTEMPTS, "BLUR_LEVELS needs one entry per attempt"
assert len(PIXELATION_WIDTHS) == MAX_ATTEMPTS, "PIXELATION_WIDTHS needs one entry per attempt"
assert len(ZOOM_OUT_FACTORS) == MAX_ATTEMPTS, "ZOOM_OUT_FACTORS needs one entry per attempt"
assert 0 <= DAILY_RESET_HOUR_UTC <= 23, "DAILY_RESET_HOUR_UTC must be 0-23"
assert 0 <= DAILY_LEADERBOARD_HOUR_UTC <= 23, "DAILY_LEADERBOARD_HOUR_UTC must be 0-23"
assert 0 <= DAILY_LEADERBOARD_MINUTE_UTC <= 59, "DAILY_LEADERBOARD_MINUTE_UTC must be 0-59"
assert 0 <= DIFFICULTY_CHALLENGE_HOUR_UTC <= 23, "DIFFICULTY_CHALLENGE_HOUR_UTC must be 0-23"
assert 0 <= DIFFICULTY_CHALLENGE_MINUTE_UTC <= 59, "DIFFICULTY_CHALLENGE_MINUTE_UTC must be 0-59"
assert 0 <= ZOOM_CHALLENGE_HOUR_UTC <= 23, "ZOOM_CHALLENGE_HOUR_UTC must be 0-23"
assert 0 <= ZOOM_CHALLENGE_MINUTE_UTC <= 59, "ZOOM_CHALLENGE_MINUTE_UTC must be 0-59"
assert all(0 <= day <= 6 for day in ZOOM_CHALLENGE_WEEKDAYS_UTC), "Zoom weekdays must be 0-6"
assert 1 <= AUTOCOMPLETE_MAX_RESULTS <= 25, "Discord allows at most 25 autocomplete suggestions"
assert SPEED_BONUS_MAX >= 0, "SPEED_BONUS_MAX cannot be negative"
assert SPEED_BONUS_INTERVAL_SECONDS > 0, "SPEED_BONUS_INTERVAL_SECONDS must be positive"
assert SPEED_BONUS_STEP >= 0, "SPEED_BONUS_STEP cannot be negative"
assert SPEED_BONUS_CUTOFF_SECONDS > 0, "SPEED_BONUS_CUTOFF_SECONDS must be positive"
assert all(width >= 2 for width in PIXELATION_WIDTHS), "Pixelation widths must be at least 2"
assert all(factor >= 1.0 for factor in ZOOM_OUT_FACTORS), "Zoom-out factors must be >= 1.0"
assert MAP_INTERVAL_HOURS * 60 >= 1, "MAP_INTERVAL_HOURS must be at least 1 minute (1 / 60)"
