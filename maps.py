import asyncio
import difflib
import html
import json
import logging
import os
import re
import unicodedata

import aiohttp

import config

log = logging.getLogger(__name__)


_ID_RE = re.compile(
    r"imgur\.com/(?:gallery/)?(?:[^/?#]*-)?([A-Za-z0-9]{5,10})(?:\.[A-Za-z]{3,4})?/?(?:[?#].*)?$"
)


def imgur_id(url):
    m = _ID_RE.search(url or "")
    return m.group(1) if m else None


def normalize(text):
    text = unicodedata.normalize("NFKD", str(text)).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[\W_]+", " ", text).strip()
    text = text.removeprefix("the ")
    return text.replace(" ", "")


def _ascii_part(name):
    """'闲暇时光 Leisure time' -> ' Leisure time', so players can guess the English part alone."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\x00-\x7F]+", " ", text)


def _answers(name, aliases):
    variants = [name, *aliases]
    ascii_name = _ascii_part(name)
    if len(normalize(ascii_name)) >= 3:
        variants.append(ascii_name)
    return {a for a in map(normalize, variants) if a}


def creators_text(game_map):
    names = [c.get("username") for c in game_map.get("creators") or [] if c.get("username")]
    return ", ".join(names) if names else "Unknown"


def parse_maps(raw):
    """Turn the API's JSON into playable map dicts."""
    if isinstance(raw, dict):                 # accept {"levels": [...]} style wrappers too
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        raise TypeError("unexpected API format")
    maps, skipped = [], 0
    for m in raw:
        if not isinstance(m, dict):
            skipped += 1
            continue
        m = dict(m)                           # copy: never modify the raw API data
        m["name"] = html.unescape(str(m.get("name") or "")).strip()   # "Aren&#39;t" -> "Aren't"
        mid = imgur_id(m.get("imgur_url"))
        if (
            m.get("hidden")
            or (config.EXCLUDE_UNDER_REVIEW and m.get("under_review"))
            or (config.ONLY_TOURNAMENT_LEGAL and not m.get("tournament_legal"))
            or not mid
            or not m["name"]
        ):
            skipped += 1
            continue
        m["id"] = mid
        m["_answers"] = _answers(m["name"], m.get("aliases", []))
        if not m["_answers"]:                 # e.g. a name made only of symbols: nobody can type it
            skipped += 1
            continue
        maps.append(m)
    log.info("Loaded %d maps (%d skipped)", len(maps), skipped)
    return maps


def _read_saved():
    with open(config.MAPS_FALLBACK_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save(raw):
    os.makedirs(os.path.dirname(config.MAPS_FALLBACK_FILE) or ".", exist_ok=True)
    tmp = config.MAPS_FALLBACK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    os.replace(tmp, config.MAPS_FALLBACK_FILE)


async def load_maps(session):
    """Fetch from the API and save a copy; if the API fails, use the saved copy."""
    try:
        async with session.get(
            config.MAPS_URL,
            headers={"User-Agent": "GuessTheMapBot/1.0", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            r.raise_for_status()
            raw = await r.json(content_type=None)
        maps = parse_maps(raw)
        if not maps:
            raise ValueError("API returned no playable maps")
    except (TimeoutError, aiohttp.ClientError, ValueError) as e:
        log.warning("Couldn't fetch %s (%s); using the saved copy", config.MAPS_URL, e)
        return parse_maps(await asyncio.to_thread(_read_saved))

    try:
        await asyncio.to_thread(_save, raw)
    except OSError as e:
        log.warning("Couldn't save the maps copy: %s", e)
    return maps


def build_suggestions(maps):
    """Unique map names (with their normalized form) for autocomplete, sorted A-Z."""
    seen, index = set(), []
    for m in maps:
        key = m["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        index.append((m["name"][:100], normalize(m["name"])))     # Discord: max 100 characters
    index.sort(key=lambda item: item[0].casefold())
    return index


def suggest(index, current, limit):
    """Names starting with what was typed come first, then names that merely contain it."""
    q = normalize(current)
    if len(q) < config.AUTOCOMPLETE_MIN_CHARS:
        return []
    starts, contains = [], []
    for name, norm in index:
        if norm.startswith(q):
            starts.append(name)
        elif q in norm:
            contains.append(name)
    return (starts + contains)[:limit]


def is_match(guess, game_map):
    g = normalize(guess)
    if not g:
        return False
    for cand in game_map["_answers"]:
        if g == cand:
            return True
        # fuzzy matching only for longer names, so short names can't be guessed by accident
        if len(cand) > 5 and abs(len(g) - len(cand)) <= 3:
            sm = difflib.SequenceMatcher(None, g, cand)
            if sm.real_quick_ratio() >= config.FUZZY_THRESHOLD and sm.ratio() >= config.FUZZY_THRESHOLD:
                return True
    return False
