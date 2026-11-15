"""Image download, caching and rendering for Guess the Map."""
import asyncio
import io
import logging
import os
import random
import secrets

import aiohttp
import discord
from PIL import Image, ImageFilter, ImageOps, ImageStat

import config

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GuessTheMapBot/1.0)"}
_sem = asyncio.Semaphore(3)          # at most 3 simultaneous downloads
_MAX_BYTES = 15 * 1024 * 1024


def cache_path(game_map):
    """Use the original JPEG cache format."""
    return os.path.join(config.CACHE_DIR, f"{game_map['id']}.jpg")


def _previous_png_cache_path(game_map):
    """Path used by the temporary palette-preserving blur update."""
    return os.path.join(config.CACHE_DIR, f"{game_map['id']}.png")


def _process(raw, dest):
    """Original image preparation: RGB conversion, downscale and JPEG cache."""
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        im = im.convert("RGB")

    if im.width < 100 or im.height < 100:
        raise ValueError("image too small (probably Imgur's 'removed' placeholder)")
    if im.width > config.MAX_IMAGE_WIDTH:
        height = round(im.height * config.MAX_IMAGE_WIDTH / im.width)
        im = im.resize((config.MAX_IMAGE_WIDTH, height), Image.Resampling.LANCZOS)

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    im.save(tmp, "JPEG", quality=88, optimize=True)
    os.replace(tmp, dest)


def _convert_png_cache(source_path, dest):
    """Restore a PNG cache entry to the original RGB/JPEG cache format."""
    with Image.open(source_path) as im:
        im.load()
        converted = im.convert("RGB")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    converted.save(tmp, "JPEG", quality=88, optimize=True)
    os.replace(tmp, dest)


async def ensure_image(session, game_map):
    """Return the cached file path, downloading it first if needed."""
    path = cache_path(game_map)
    if os.path.exists(path):
        return path

    os.makedirs(config.CACHE_DIR, exist_ok=True)
    async with _sem:
        # Recheck after waiting for the download semaphore.
        if os.path.exists(path):
            return path

        # Keep using existing JPEGs untouched. If only the PNG cache from the
        # temporary blur experiment exists, convert it to the original format.
        previous_png = _previous_png_cache_path(game_map)
        if os.path.exists(previous_png):
            try:
                await asyncio.to_thread(_convert_png_cache, previous_png, path)
                return path
            except (OSError, ValueError) as exc:
                log.warning("Couldn't restore previous cache for %s: %s", game_map["name"], exc)

        for ext in ("png", "jpg"):
            url = f"https://i.imgur.com/{game_map['id']}.{ext}"
            try:
                async with session.get(
                    url,
                    headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status == 429:
                        log.warning("Imgur rate limit hit while fetching %s", game_map["name"])
                        return None
                    if response.status != 200 or "removed" in str(response.url):
                        continue
                    if (response.content_length or 0) > _MAX_BYTES:
                        continue
                    raw = await response.read()

                await asyncio.to_thread(_process, raw, path)
                return path
            except (TimeoutError, aiohttp.ClientError, ValueError, OSError) as exc:
                log.warning("Fetch failed for %s (%s): %s", game_map["name"], url, exc)

    return None


def _fit_blur_width(im):
    """Normalize width so blur strength is independent of source dimensions."""
    reference_width = int(config.BLUR_REFERENCE_WIDTH)
    if im.width == reference_width:
        return im
    height = max(1, round(im.height * reference_width / im.width))
    return im.resize((reference_width, height), Image.Resampling.LANCZOS)


def _blur_image(im, level_index):
    """The original blur: normalized width + contrast adjustment + Gaussian blur.

    This intentionally does not quantize colors or constrain the result to a
    sampled palette. It restores the earlier appearance while keeping the blur
    strength consistent between images of different dimensions.
    """
    im = _fit_blur_width(im.convert("RGB"))

    sample = im.copy()
    sample.thumbnail((128, 128), Image.Resampling.LANCZOS)
    contrast = sum(ImageStat.Stat(sample).stddev) / 3

    # Low-color-variation images receive a somewhat gentler blur.
    contrast_factor = 0.30 + 0.70 * min(contrast / 60.0, 1.0)

    levels = config.BLUR_LEVELS
    if not levels:
        raise ValueError("BLUR_LEVELS cannot be empty")
    level = levels[min(level_index, len(levels) - 1)]
    radius = max(
        1.0,
        float(level) * float(config.BLUR_MULTIPLIER)
        * float(config.BLUR_REFERENCE_WIDTH) * contrast_factor,
    )
    return im.filter(ImageFilter.GaussianBlur(radius))


def _blur(path, level_index):
    """Return the original blurred JPEG bytes."""
    with Image.open(path) as source:
        im = source.convert("RGB")

    result = _blur_image(im, level_index)
    buf = io.BytesIO()
    result.save(buf, "JPEG", quality=80)
    buf.seek(0)
    return buf


def _pixelate_image(im, target_width):
    original_size = im.size
    target_width = max(2, int(target_width))
    target_height = max(2, round(im.height * target_width / im.width))
    small = im.resize((target_width, target_height), Image.Resampling.BOX)
    return small.resize(original_size, Image.Resampling.NEAREST)


def _pixelate(path, attempt_index, invert=False):
    with Image.open(path) as source:
        im = source.convert("RGB")

    levels = tuple(getattr(config, "PIXELATION_WIDTHS", (28, 56, 112)))
    target_width = levels[min(attempt_index, len(levels) - 1)]
    result = _pixelate_image(im, target_width)
    if invert:
        result = ImageOps.invert(result)
    return result


def _zoom_out(path, attempt_index):
    with Image.open(path) as source:
        im = source.convert("RGB")

    factors = tuple(getattr(config, "ZOOM_OUT_FACTORS", (4.0, 2.5, 1.5)))
    factor = max(1.0, float(factors[min(attempt_index, len(factors) - 1)]))
    crop_width = max(1, round(im.width / factor))
    crop_height = max(1, round(im.height / factor))
    left = (im.width - crop_width) // 2
    top = (im.height - crop_height) // 2
    crop = im.crop((left, top, left + crop_width, top + crop_height))
    return crop.resize(im.size, Image.Resampling.LANCZOS)


def _zoom_in_image(im, factor):
    """Crop around the centre and scale back up, making the map appear zoomed in."""
    factor = max(1.0, float(factor))
    crop_width = max(1, round(im.width / factor))
    crop_height = max(1, round(im.height / factor))
    left = (im.width - crop_width) // 2
    top = (im.height - crop_height) // 2
    crop = im.crop((left, top, left + crop_width, top + crop_height))
    return crop.resize(im.size, Image.Resampling.LANCZOS)


def _render_mode(path, mode, attempt_index):
    """Render scheduled modes using the agreed difficulty definitions."""
    if mode == "easy":
        # Easy = pixelated only.
        return _pixelate(path, attempt_index, invert=False)

    if mode in ("normal", "hard", "impossible"):
        with Image.open(path) as source:
            im = source.convert("RGB")
        # Normal = original blur. Hard adds inversion. Impossible adds a centre zoom.
        result = _blur_image(im, attempt_index)
        if mode in ("hard", "impossible"):
            result = ImageOps.invert(result)
        if mode == "impossible":
            factor = float(getattr(config, "IMPOSSIBLE_ZOOM_FACTOR", 2.5))
            result = _zoom_in_image(result, factor)
        return result

    if mode == "zoom":
        return _zoom_out(path, attempt_index)
    raise ValueError(f"Unsupported game mode: {mode}")


async def _file_from_render(path, mode, attempt_index):
    result = await asyncio.to_thread(_render_mode, path, mode, attempt_index)
    buf = io.BytesIO()
    if mode == "normal":
        # Keep the exact original blur's JPEG output characteristics.
        result.save(buf, "JPEG", quality=80)
        extension = "jpg"
    else:
        result.save(buf, "PNG", optimize=True)
        extension = "png"
    buf.seek(0)
    return discord.File(buf, filename=f"{secrets.token_hex(6)}.{extension}")


_INFINITE_FILTER_NAMES = ("zoom", "pixelated", "blurred", "inverted")


def random_infinite_effects():
    """Choose a random non-empty combination of supported visual filters."""
    chosen = random.sample(_INFINITE_FILTER_NAMES, random.randint(1, len(_INFINITE_FILTER_NAMES)))
    effects = {}
    if "zoom" in chosen:
        effects["zoom_factor"] = random.choice((1.5, 2.0, 2.5, 3.0, 4.0))
    if "pixelated" in chosen:
        effects["pixel_width"] = random.choice((16, 22, 28, 36, 48, 64))
    if "blurred" in chosen:
        effects["blur_index"] = random.randrange(len(config.BLUR_LEVELS))
    if "inverted" in chosen:
        effects["inverted"] = True
    return effects


def describe_infinite_effects(effects, translate=None):
    """Describe applied Infinite filters using the bot's messages.json templates.

    ``translate`` is normally ``bot.t``. Defaults remain available for direct
    callers and older test scripts that use this image module independently.
    """
    def message(key, fallback, **values):
        return translate(key, default=fallback, **values) if translate else fallback.format(**values)

    labels = []
    if "zoom_factor" in effects:
        labels.append(message(
            "infinite.effects.zoom", "Zoomed in ×{factor:g}", factor=effects["zoom_factor"]
        ))
    if "pixel_width" in effects:
        labels.append(message(
            "infinite.effects.pixelated", "Pixelated ({width} px detail width)",
            width=effects["pixel_width"],
        ))
    if "blur_index" in effects:
        level = config.BLUR_LEVELS[effects["blur_index"]]
        labels.append(message(
            "infinite.effects.blurred", "Blurred (level {level_index}, {strength:g})",
            level_index=effects["blur_index"] + 1, strength=level,
        ))
    if effects.get("inverted"):
        labels.append(message("infinite.effects.inverted", "Colours inverted"))
    return labels


def _render_infinite(path, effects):
    with Image.open(path) as source:
        im = source.convert("RGB")

    # Fixed, documented processing order keeps combinations reproducible.
    if "zoom_factor" in effects:
        im = _zoom_in_image(im, effects["zoom_factor"])
    if "blur_index" in effects:
        im = _blur_image(im, int(effects["blur_index"]))
    if "pixel_width" in effects:
        im = _pixelate_image(im, int(effects["pixel_width"]))
    if effects.get("inverted"):
        im = ImageOps.invert(im)
    return im


async def infinite_file(path, effects):
    """Render one random-filter map. This has no scoring or persistence side effects."""
    result = await asyncio.to_thread(_render_infinite, path, dict(effects))
    buf = io.BytesIO()
    result.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return discord.File(buf, filename=f"{secrets.token_hex(6)}.png")


async def blurred_file(path, level_index):
    """Return a discord.File using the restored original blur algorithm."""
    buf = await asyncio.to_thread(_blur, path, level_index)
    return discord.File(buf, filename=f"{secrets.token_hex(6)}.jpg")


async def mode_file(path, mode, attempt_index):
    """Return an image for easy/normal/hard/impossible/zoom challenges."""
    return await _file_from_render(path, mode, attempt_index)
