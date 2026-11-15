# Guess the Map – Discord bot

Posts a heavily blurred map image; players guess the name privately (slash command or button). 3 tries each, the picture gets clearer after every
wrong guess. Correct on try 1/2/3 = 3/2/1 points. First correct guess ends the round and reveals the map
(name, creator, code, full image). Points are kept in a per-server leaderboard.

## 1\. Create the bot

1. https://discord.com/developers/applications → **New Application** → **Bot** tab → **Reset Token** (copy it).
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions: View Channels, Send Messages,
Attach Files, Embed Links, Read Message History. (No privileged intents needed.) Open the generated URL to invite the bot.

## 2\. Run it

```
pip install -r requirements.txt
cp .env.example .env          # put your token in .env  (on a host, set DISCORD\_TOKEN as an env variable)
python prefetch.py            # optional but recommended: caches all images
python bot.py
```

