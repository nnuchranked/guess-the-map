"""All the knobs in one place. Edit and restart the bot."""

# --- Game rules ---------------------------------------------------------
MAX_ATTEMPTS = 3                      # tries per player, per round
POINTS_BY_ATTEMPT = [3, 2, 1]         # points for a correct guess on try 1 / 2 / 3
BLUR_LEVELS = [0.05, 0.035, 0.015]    # blur radius as a fraction of image width, per try
                                      # (bigger = blurrier). One entry per attempt.
ROUND_SECONDS = 86400                   # a round ends (answer revealed) after this long
DEFAULT_INTERVAL_MINUTES = 1440        # default gap between automatic rounds
RECENT_MAPS_MEMORY = 50               # don't repeat any of the last N maps

# --- Guess handling (guesses are private: /guess or the button) ---------
FUZZY_THRESHOLD = 0.85                # 1.0 = exact only; lower = more typo-tolerant
MAX_GUESS_LENGTH = 60                 # longest allowed guess text

# --- Images / files -----------------------------------------------------
MAX_IMAGE_WIDTH = 800                 # cached images are downscaled to this width (saves RAM/disk)
MAPS_URL = "https://compuch.com/api/levels"
MAPS_REFRESH_HOURS = 6                      # re-fetch the list this often
MAPS_FALLBACK_FILE = "data/maps_cache.json" # last good copy, used if the API is down
MESSAGES_FILE = "messages.json"
CACHE_DIR = "cache"
DB_FILE = "data/scores.db"

assert len(POINTS_BY_ATTEMPT) == MAX_ATTEMPTS, "POINTS_BY_ATTEMPT needs one entry per attempt"
assert len(BLUR_LEVELS) == MAX_ATTEMPTS, "BLUR_LEVELS needs one entry per attempt"
