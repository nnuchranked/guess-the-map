"""JSON-backed, reloadable user-facing text for the Guess the Map bot."""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Compatibility for older flat messages.json files. New customisations should
# use the nested keys in the supplied messages.json; these aliases preserve
# the most common legacy game-result strings during migration.
_LEGACY_ALIASES = {
    "main.correct_first": "correct_first",
    "main.correct_second": "correct_second",
    "main.correct_third": "correct_third",
    "main.wrong_guess": "wrong_guess",
    "main.out_of_attempts": "out_of_attempts",
    "main.day_over": "day_over",
    "main.announce_correct": "announce_correct",
}


class _SafeFormat(dict):
    """Leave unknown {placeholders} untouched instead of crashing a response."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.missing: set[str] = set()

    def __missing__(self, key: str) -> str:
        self.missing.add(key)
        return "{" + key + "}"


class Messages:
    """Load strings from messages.json using dotted keys such as ``main.day_over``.

    Values may be strings or lists of strings. Lists are useful for random
    variants. ``pick`` remains available for compatibility with older bot code.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {}
        self._warned_missing: set[str] = set()
        self.load()

    def load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if not isinstance(loaded, dict):
                raise ValueError("messages.json must contain a JSON object at its root")
            self.data = loaded
            log.info("Loaded message text from %s", self.path)
        except FileNotFoundError:
            self.data = {}
            log.warning("%s was not found; built-in fallback text will be used", self.path)
        except (OSError, json.JSONDecodeError, ValueError):
            log.exception("Could not load %s; keeping the previous messages", self.path)
            if not isinstance(self.data, dict):
                self.data = {}

    def _lookup(self, key: str) -> Any:
        # First accept literal dotted keys for easier migration, then nested JSON.
        if key in self.data:
            return self.data[key]
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is not None:
            return node
        legacy_key = _LEGACY_ALIASES.get(key)
        if legacy_key:
            return self.data.get(legacy_key)
        return None

    def has(self, key: str) -> bool:
        return self._lookup(key) is not None

    def text(self, key: str, default: str | None = None, **values: Any) -> str:
        value = self._lookup(key)
        if value is None:
            value = default if default is not None else key.replace(".", " ").replace("_", " ").capitalize()
        if isinstance(value, list):
            choices = [item for item in value if isinstance(item, str)]
            value = random.choice(choices) if choices else (default or key)
        if isinstance(value, dict):
            # Optional structured form: {"text": "..."}.
            value = value.get("text", default if default is not None else key)
        if not isinstance(value, (str, int, float)):
            value = default if default is not None else key.replace(".", " ").replace("_", " ").capitalize()
        try:
            safe = _SafeFormat(values)
            result = str(value).format_map(safe)
            if safe.missing and key not in self._warned_missing:
                # A caller forgot to pass values for this template, so players would see raw
                # {placeholders}. Log it once per key so it can't go unnoticed.
                self._warned_missing.add(key)
                log.warning("Message %s was shown with unfilled placeholders: %s",
                            key, ", ".join(sorted(safe.missing)))
            return result
        except (ValueError, TypeError):
            log.exception("Could not format message key %s", key)
            return str(value)

    def pick(self, key: str, default: str | None = None, **values: Any) -> str:
        """Backward-compatible alias used by older versions of bot.py."""
        return self.text(key, default=default, **values)