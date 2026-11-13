import asyncio
import io
import logging
import os
import secrets

import aiohttp
import discord
from PIL import Image, ImageFilter

import config

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GuessTheMapBot/1.0)"}
_sem = asyncio.Semaphore(3)          # at most 3 simultaneous downloads
_MAX_BYTES = 15 * 1024 * 1024


def cache_path(game_map):
    return os.path.join(config.CACHE_DIR, f"{game_map['id']}.jpg")


def _process(raw, dest):
    """Decode, downscale, strip metadata, store as a small JPEG."""
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        im = im.convert("RGB")
    if im.width < 100 or im.height < 100:
        raise ValueError("image too small (probably Imgur's 'removed' placeholder)")
    if im.width > config.MAX_IMAGE_WIDTH:
        h = round(im.height * config.MAX_IMAGE_WIDTH / im.width)
        im = im.resize((config.MAX_IMAGE_WIDTH, h), Image.LANCZOS)
    tmp = dest + ".tmp"
    im.save(tmp, "JPEG", quality=88, optimize=True)
    os.replace(tmp, dest)


async def ensure_image(session, game_map):
    """Return the cached file path, downloading it first if needed. None on failure."""
    path = cache_path(game_map)
    if os.path.exists(path):
        return path
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    async with _sem:
        for ext in ("png", "jpg"):
            url = f"https://i.imgur.com/{game_map['id']}.{ext}"
            try:
                async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 429:
                        log.warning("Imgur rate limit hit while fetching %s", game_map["name"])
                        return None
                    if r.status != 200 or "removed" in str(r.url):
                        continue
                    if (r.content_length or 0) > _MAX_BYTES:
                        continue
                    raw = await r.read()
                await asyncio.to_thread(_process, raw, path)
                return path
            except (TimeoutError, aiohttp.ClientError, ValueError, OSError) as e:
                log.warning("Fetch failed for %s (%s): %s", game_map["name"], url, e)
    return None


def _blur(path, level_index):
    with Image.open(path) as im:
        radius = max(1.0, config.BLUR_LEVELS[level_index] * im.width)
        out = im.filter(ImageFilter.GaussianBlur(radius))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=80)
    buf.seek(0)
    return buf


async def blurred_file(path, level_index):
    """A discord.File with a random filename."""
    buf = await asyncio.to_thread(_blur, path, level_index)
    return discord.File(buf, filename=f"{secrets.token_hex(6)}.jpg")
