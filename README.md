# provas-tracker

Pings Telegram when [Garrafeira Pepe](https://garrafeirapepe.pt/provas/) publishes a new wine tasting. Runs every 5 minutes on GitHub Actions.

## How it works

Their booking system uses the Amelia plugin, which exposes an unauthenticated AJAX endpoint listing all events. The script paginates that endpoint, diffs the IDs against `state.json`, and sends a Telegram message for each new event with a future date. The message includes the title, date, price, and the wines being poured (parsed from the event description).

Failed sends are not committed to state, so they retry on the next run. Past-dated events are recorded silently to avoid noise. Each Actions run writes a summary table of all known events to the workflow page.

## Setup

1. Fork this repo (keep it public; Actions minutes are unmetered for public repos).
2. Create a Telegram bot via [@BotFather](https://t.me/BotFather), copy the token. Send `/start` to the bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat.id`.
3. Add two repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Trigger the workflow once from the Actions tab to verify. Cron takes over from there.

## Local dev

```bash
python3 main.py                                              # dry-run, no notifications
TELEGRAM_BOT_TOKEN=… TELEGRAM_CHAT_ID=… python3 main.py      # real send
```

A dry-run prints the formatted message to stdout but does **not** mark events seen, so the next CI run still notifies them.

## State

`state.json` is committed back by the workflow on every change. The git log doubles as an audit trail of what the tracker saw and when.
