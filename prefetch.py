"""Optional: download + shrink every map image up front, so rounds start instantly.
Run once:  python prefetch.py   (safe to re-run; already-cached images are skipped)"""
import asyncio
import logging

import aiohttp

import images
from maps import load_maps

logging.basicConfig(level=logging.INFO)


async def main():
    async with aiohttp.ClientSession() as session:
        maps = await load_maps(session)
        ok = 0
        for i, m in enumerate(maps, 1):
            if await images.ensure_image(session, m):
                ok += 1
            else:
                print(f"FAILED: {m['name']} ({m['imgur_url']})")
            if i % 25 == 0:
                print(f"{i}/{len(maps)}")
            await asyncio.sleep(0.4)             # be gentle with Imgur
    print(f"Done: {ok}/{len(maps)} cached in ./cache")


asyncio.run(main())
