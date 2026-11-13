"""All the knobs in one place. Edit and restart the bot."""

# --- Game rules ---------------------------------------------------------
MAX_ATTEMPTS = 3                      # tries per player, per round
POINTS_BY_ATTEMPT = [3, 2, 1]         # points for a correct guess on try 1 / 2 / 3
BLUR_LEVELS = [0.06, 0.04, 0.02]    # blur radius as a fraction of image width, per try
                                      # (bigger = blurrier). One entry per attempt.
                                      # Radius in pixels = level x BLUR_MULTIPLIER x 800.
BLUR_MULTIPLIER = 1.0                 # scales ALL levels at once: 0.5 = half as blurry, 2 = twice
DAILY_RESET_HOUR_UTC = 0              # a new map appears every day at this hour (0-23, UTC)
ANNOUNCE_CORRECT = True               # post "X solved it on try N!" publicly (no spoilers)
ONLY_TOURNAMENT_LEGAL = True          # only use maps with tournament_legal == true
EXCLUDE_UNDER_REVIEW = True           # skip maps flagged under_review
RECENT_MAPS_MEMORY = 50               # don't repeat any of the last N maps

# --- Guess handling (guesses are private: /guess or the button) ---------
FUZZY_THRESHOLD = 0.85                # 1.0 = exact only; lower = more typo-tolerant
MAX_GUESS_LENGTH = 60                 # longest allowed guess text
AUTOCOMPLETE_MIN_CHARS = 2            # start suggesting map names after this many typed characters
AUTOCOMPLETE_MAX_RESULTS = 10         # suggestions shown (Discord's maximum is 25; fewer = harder)

# --- Images / files -----------------------------------------------------
MAX_IMAGE_WIDTH = 800                 # cached images are downscaled to this width
MESSAGES_FILE = "messages.json"
CACHE_DIR = "cache"
DB_FILE = "data/scores.db"

assert len(POINTS_BY_ATTEMPT) == MAX_ATTEMPTS, "POINTS_BY_ATTEMPT needs one entry per attempt"
assert len(BLUR_LEVELS) == MAX_ATTEMPTS, "BLUR_LEVELS needs one entry per attempt"
assert 0 <= DAILY_RESET_HOUR_UTC <= 23, "DAILY_RESET_HOUR_UTC must be 0-23"
assert 1 <= AUTOCOMPLETE_MAX_RESULTS <= 25, "Discord allows at most 25 autocomplete suggestions"

MAPS_URL = "https://compuch.com/api/levels"
MAPS_REFRESH_HOURS = 6
MAPS_FALLBACK_FILE = "data/maps_cache.json"