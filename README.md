# provas-tracker

Pings Telegram when [Garrafeira Pepe](https://garrafeirapepe.pt/provas/) publishes a new wine tasting.

## Architecture

The active path is a **Cloudflare Worker** (`src/index.ts`) on a 5-minute cron, with state in **Workers KV**. GitHub Actions' scheduler proved too unreliable for this — it silently drops a large fraction of scheduled runs. The Worker fetches Garrafeira Pepe's Amelia booking API, diffs event IDs against KV, and posts a Telegram message for each new event whose first period is in the future. Failed Telegram sends are not marked seen, so the next tick retries them.

The Python implementation in `python/` plus its GitHub Actions workflow (`.github/workflows/tracker.yml`) are functionally equivalent and still work; the workflow runs on `workflow_dispatch` only and serves as a manual / disaster-recovery path.

## Repo layout

```
src/index.ts              # Cloudflare Worker (active)
wrangler.toml             # Worker config + cron trigger
package.json, tsconfig.json
python/main.py            # Python equivalent (manual fallback)
python/state.json         # State for the Python path (snapshot)
.github/workflows/tracker.yml
```

## How it works

1. Hit `wp-admin/admin-ajax.php?action=wpamelia_api&call=/events&bookings=false` and paginate until empty.
2. State (the set of event IDs already handled) lives in KV under the `state` key.
3. For every new ID, check whether the event is still in the future. Past events are marked seen silently.
4. Future events get a formatted Telegram message: title, date, price, intro paragraph, list of wines, and a link to the tasting page.

## Setup

### Telegram

1. Create a bot via [@BotFather](https://t.me/BotFather), copy the token.
2. Send `/start` to the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat.id`.

### Cloudflare Worker

1. Create a KV namespace (e.g. `provas-tracker-state`) and paste its ID into `wrangler.toml` under `kv_namespaces`.
2. Connect this repo to a Cloudflare Worker. Build root is the repo root.
3. Add the following secrets in the Cloudflare dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Deploy. The cron trigger picks up the next `*/5 * * * *` tick.

## State

KV stores a single JSON document under `state`:

```json
{ "seen_ids": [1, 4, 5, ...], "last_check": "2026-04-25T..." }
```

`python/state.json` is a snapshot from when the GitHub Actions implementation was the active path.

## Local dev (Python fallback)

```bash
python3 python/main.py                                          # dry-run, no notifications
TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… python3 python/main.py  # real send
```

A dry-run prints the formatted message to stdout but does **not** mark events seen, so the next CI run still notifies them.
