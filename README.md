# Guess the Map – Discord bot

Posts a heavily blurred map image; players guess the name privately (slash command or button). 3 tries each, the picture gets clearer after every
wrong guess. Correct on try 1/2/3 = 3/2/1 points. First correct guess ends the round and reveals the map
(name, creator, code, full image). Points are kept in a per-server leaderboard.

## 1. Create the bot
1. https://discord.com/developers/applications → **New Application** → **Bot** tab → **Reset Token** (copy it).
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions: View Channels, Send Messages,
   Attach Files, Embed Links, Read Message History. (No privileged intents needed.) Open the generated URL to invite the bot.

## 2. Run it
```
pip install -r requirements.txt
cp .env.example .env          # put your token in .env  (on a host, set DISCORD_TOKEN as an env variable)
python prefetch.py            # optional but recommended: caches all images
python bot.py
```
Put your `maps.json` next to `bot.py`. Optional extra field per map: `"aliases": ["other name", ...]`.
Entries with `hidden` or `under_review` = true are skipped.

## 3. Use it
- `/guessmap setup #channel [interval_minutes]` – automatic rounds (default every 120 min).
- `/guessmap start` – start a round right now in the current channel.
- `/guess <name>` or the **Make a guess** button under each round – guesses are **private**: only you see your
  feedback and the clearer image after a wrong guess. A correct guess is announced publicly with the reveal.
- `/guessmap leaderboard` – top 10.
- `/guessmap disable`, `/guessmap reload` (re-reads maps.json and messages.json, no restart).
(Setup/start/disable/reload need the Manage Server permission.)


## 4. Tweak
- Points, tries, blur strength, round length, typo tolerance: `config.py`
- All jolly messages: `messages.json` (placeholders: {user} {points} {total} {map_name} {attempts_left} {minutes})

## 5. Free hosting notes
- Needs ~100–150 MB RAM and a few MB of disk (images are shrunk to 800px and cached as JPEG).
- Keep `data/` (scores.db) on **persistent storage**. `cache/` can be wiped; it just re-downloads.
- Some hosts' IP ranges get rate-limited by Imgur. If so, run `prefetch.py` on your own PC and upload the
  `cache/` folder with the bot; then the bot never needs to contact Imgur.
- Avoid hosts that put free processes to sleep; a Discord bot has to stay connected.
