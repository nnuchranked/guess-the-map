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


def _answers(name, aliases, authors):
    """Build valid answers: map name, aliases, authors and combined forms."""
    titles = [str(name).strip()]

    if isinstance(aliases, str):
        aliases = [aliases]

    titles.extend(str(alias).strip() for alias in (aliases or []) if str(alias).strip())

    authors = list(
        dict.fromkeys(
            str(author).strip() for author in (authors or []) if str(author).strip()
        )
    )

    variants = list(authors)

    for title in titles:
        title_variants = [title]

        # Preserve the existing support for English portions
        # of multilingual map names.
        ascii_title = _ascii_part(title).strip()

        if len(normalize(ascii_title)) >= 3 and normalize(ascii_title) != normalize(
            title
        ):
            title_variants.append(ascii_title)

        variants.extend(title_variants)

        # Accept "Map | by Author" for each individual creator.
        for title_variant in title_variants:
            for author in authors:
                variants.append(f"{title_variant} | by {author}")

        # Also accept the complete combined creator list.
        if authors:
            variants.append(f"{title} | by {', '.join(authors)}")

    answers = set()

    for variant in variants:
        normalized = normalize(variant)
        if normalized:
            answers.add(normalized)

    return answers


def _creator_names(game_map):
    """Get unique creator usernames from a map."""
    names = []
    seen = set()

    for creator in game_map.get("creators") or []:
        if not isinstance(creator, dict):
            continue

        username = str(creator.get("username") or "").strip()
        key = username.casefold()

        if username and key not in seen:
            seen.add(key)
            names.append(username)

    return names


def creators_text(game_map):
    names = _creator_names(game_map)
    return ", ".join(names) if names else "Unknown"


def map_display_name(game_map):
    """Return 'Map Name | by Creator'."""
    authors = _creator_names(game_map)

    if not authors:
        return game_map["name"]

    return f"{game_map['name']} | by {', '.join(authors)}"


def parse_maps(raw):
    """Turn the API's JSON into playable map dicts."""
    if isinstance(raw, dict):  # accept {"levels": [...]} style wrappers too
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        raise TypeError("unexpected API format")
    maps, skipped = [], 0
    for m in raw:
        if not isinstance(m, dict):
            skipped += 1
            continue
        m = dict(m)  # copy: never modify the raw API data
        m["name"] = html.unescape(
            str(m.get("name") or "")
        ).strip()  # "Aren&#39;t" -> "Aren't"
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
        m["_answers"] = _answers(
            m["name"],
            m.get("aliases", []),
            _creator_names(m),
        )
        if not m["_answers"]:  # e.g. a name made only of symbols: nobody can type it
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
    """Unique map names, creator names and combined names for autocomplete."""
    seen, index = set(), []

    for m in maps:
        authors = _creator_names(m)

        candidates = [
            m["name"],
            *authors,
        ]

        # Combined map + creator suggestions.
        candidates.extend(
            f"{m['name']} | by {author}"
            for author in authors
        )

        if authors:
            candidates.append(
                f"{m['name']} | by {', '.join(authors)}"
            )

        for candidate in candidates:
            candidate = str(candidate).strip()
            if not candidate:
                continue

            # Discord limits autocomplete choice names to 100 characters.
            candidate = candidate[:100]
            key = candidate.casefold()

            if key in seen:
                continue

            seen.add(key)
            index.append((candidate, normalize(candidate)))

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
            if (
                sm.real_quick_ratio() >= config.FUZZY_THRESHOLD
                and sm.ratio() >= config.FUZZY_THRESHOLD
            ):
                return True
    return False
