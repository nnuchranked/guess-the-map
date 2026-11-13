import json
import random
from collections import defaultdict


class Messages:
    """Loads messages.json. Placeholders: {user} {points} {total} {map_name} {attempts_left} {minutes}"""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)          # if this raises, the old messages stay in use
        self.data = data

    def pick(self, key, **values):
        options = self.data.get(key) or ["..."]
        return random.choice(options).format_map(defaultdict(str, values))
