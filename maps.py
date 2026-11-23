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

# Common Roman-numeral sequel markers. Keep this deliberately bounded so
# ordinary words such as "mix" are not mistaken for version numbers.
_ROMAN_SUFFIXES = {
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
}


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


def _title_variants(name, aliases):
    """Return map titles/aliases and their supported English-only variants."""
    titles = [str(name).strip()]

    if isinstance(aliases, str):
        aliases = [aliases]

    titles.extend(str(alias).strip() for alias in (aliases or []) if str(alias).strip())

    variants = []
    for title in titles:
        variants.append(title)

        # Preserve the existing support for English portions of multilingual names.
        ascii_title = _ascii_part(title).strip()
        if len(normalize(ascii_title)) >= 3 and normalize(ascii_title) != normalize(title):
            variants.append(ascii_title)

    return titles, variants


def _answers(name, aliases, authors):
    """Build exact valid answers: names, aliases, authors and combined forms."""
    titles, _ = _title_variants(name, aliases)
    authors = list(
        dict.fromkeys(
            str(author).strip() for author in (authors or []) if str(author).strip()
        )
    )

    variants = list(authors)

    for title in titles:
        ascii_title = _ascii_part(title).strip()
        title_variants = [title]
        if len(normalize(ascii_title)) >= 3 and normalize(ascii_title) != normalize(title):
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


def _fuzzy_answers(name, aliases, authors):
    """Answers eligible for typo tolerance; never include title+creator combinations."""
    _, title_variants = _title_variants(name, aliases)
    variants = title_variants + list(authors or [])
    return {
        key: _numbered_suffix(value)
        for value in variants
        if (key := normalize(value))
    }


def _numbered_suffix(text):
    """Return a trailing numeric/Roman version marker, if the name has one."""
    decomposed = unicodedata.normalize("NFKD", str(text)).casefold()
    decomposed = "".join(c for c in decomposed if not unicodedata.combining(c))
    tokens = re.findall(r"[a-z0-9]+", decomposed)
    if not tokens:
        return None

    last = tokens[-1]
    if last.isdigit() or last in _ROMAN_SUFFIXES:
        return last
    return None


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
        authors = _creator_names(m)
        m["_answers"] = _answers(m["name"], m.get("aliases", []), authors)
        m["_fuzzy_answers"] = _fuzzy_answers(m["name"], m.get("aliases", []), authors)
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


def _parse_saved():
    return parse_maps(_read_saved())


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
        maps = await asyncio.to_thread(parse_maps, raw)    # CPU-heavy: keep it off the event loop
        if not maps:
            raise ValueError("API returned no playable maps")
    except (TimeoutError, aiohttp.ClientError, ValueError) as e:
        log.warning("Couldn't fetch %s (%s); using the saved copy", config.MAPS_URL, e)
        return await asyncio.to_thread(_parse_saved)

    try:
        await asyncio.to_thread(_save, raw)
    except OSError as e:
        log.warning("Couldn't save the maps copy: %s", e)
    return maps


def build_suggestions(maps):
    """Build searchable suggestions with separate title and creator fields.

    Keeping these fields separate lets creator searches surface maps whose titles
    contain no searchable letters (for example, a title consisting only of ▓).
    """
    seen, index = set(), []

    for game_map in maps:
        authors = _creator_names(game_map)
        title = str(game_map.get("name") or "").strip()
        candidate = (
            f"{title} | by {', '.join(authors)}"
            if authors
            else title
        )[:100]

        # Deduplicate by the visible candidate, not its normalized form. Two
        # different symbol-only titles can normalize identically while still
        # being distinct, selectable map names.
        visible_key = candidate.casefold()
        if not candidate or visible_key in seen:
            continue
        seen.add(visible_key)

        key = normalize(candidate)
        title_key = normalize(title)
        creator_keys = tuple(dict.fromkeys(
            normalized for author in authors if (normalized := normalize(author))
        ))
        if not key and not title_key and not creator_keys:
            continue

        index.append((candidate, key, title_key, creator_keys))

    index.sort(key=lambda item: item[0].casefold())
    return index


def suggest(index, current, limit):
    """Return relevant map suggestions, prioritizing creator-only matches for symbol titles."""
    q = normalize(current)
    if len(q) < config.AUTOCOMPLETE_MIN_CHARS:
        return []

    title_starts, title_contains = [], []
    unsearchable_title_creator_matches = []
    creator_matches = []
    combined_starts, combined_contains = [], []

    for item in index:
        # Accept legacy two-field indexes as well as the richer index generated
        # by build_suggestions above.
        name, norm = item[0], item[1]
        if len(item) >= 4:
            title_norm, creator_norms = item[2], item[3]
        else:
            title_norm, creator_norms = normalize(name.split(" | by ", 1)[0]), ()

        if title_norm.startswith(q):
            title_starts.append(name)
        elif title_norm and q in title_norm:
            title_contains.append(name)

        creator_hit = any(author.startswith(q) or q in author for author in creator_norms)
        if creator_hit:
            if not title_norm:
                unsearchable_title_creator_matches.append(name)
            else:
                creator_matches.append(name)

        if norm.startswith(q):
            combined_starts.append(name)
        elif q in norm:
            combined_contains.append(name)

    ordered = (
        title_starts
        + title_contains
        + unsearchable_title_creator_matches
        + creator_matches
        + combined_starts
        + combined_contains
    )

    # A candidate can match both its title and creator. Keep only its first,
    # highest-priority occurrence before applying Discord's result limit.
    unique = list(dict.fromkeys(ordered))
    return unique[:max(0, int(limit))]


def is_match(guess, game_map):
    g = normalize(guess)
    if not g:
        return False

    # Exact matching remains available for every supported answer form, including
    # aliases, creator names, and "Map | by Creator".
    for cand in game_map["_answers"]:
        if g == cand:
            return True

    # Fuzzy matching is limited to standalone titles, aliases, English title
    # portions, and standalone creator names. Combined title+creator answers are
    # exact-only so they cannot make a shorter/different map name look correct.
    fuzzy_answers = game_map.get("_fuzzy_answers")
    if fuzzy_answers is None:  # compatibility with map dictionaries created by older code
        fuzzy_answers = _fuzzy_answers(
            game_map.get("name", ""),
            game_map.get("aliases", []),
            _creator_names(game_map),
        )

    guess_suffix = _numbered_suffix(guess)
    for cand, cand_suffix in fuzzy_answers.items():
        # A missing, added, or changed trailing number/Roman numeral is meaningful.
        if guess_suffix != cand_suffix and (guess_suffix is not None or cand_suffix is not None):
            continue

        # Prefix differences commonly indicate a distinct sequel/sub-map name.
        # Typos elsewhere are still handled by the fuzzy comparison below.
        if g.startswith(cand) or cand.startswith(g):
            continue

        # Fuzzy matching only for longer names, so short names can't be guessed by accident.
        if len(cand) > 5 and abs(len(g) - len(cand)) <= 3:
            sm = difflib.SequenceMatcher(None, g, cand)
            if (
                sm.real_quick_ratio() >= config.FUZZY_THRESHOLD
                and sm.ratio() >= config.FUZZY_THRESHOLD
            ):
                return True
    return False