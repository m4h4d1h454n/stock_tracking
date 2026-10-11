# DSE Portfolio Tracker Bot

Track your Dhaka Stock Exchange investments from Telegram: log buys/sells,
see live P/L, set price alerts, and pull reports. No database server —
everything is stored in two small JSON files under `data/`.

## How it works

- **`dse_client.py`** — scrapes live prices from dsebd.org's public price
  table (DSE has no official API, so this is the standard approach).
  Cached 60s so it doesn't hammer the site.
- **`portfolio.py`** — every buy/sell is appended to `data/portfolio.json`.
  Holdings, average cost, and realized P/L are all derived from that log,
  so the log is your single source of truth (and your audit trail).
  Dividends live in the same file: cash dividends in a `dividends` list,
  stock dividends as zero-cost `bonus` transactions.
- **`alerts.py`** — price alerts (`above`/`below` a target) stored in
  `data/alerts.json`.
- **`bot.py`** — the Telegram bot itself, plus a background job that
  re-checks alerts every 5 minutes (configurable) and pushes you a message
  when one triggers.

## Setup

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Telegram bot**
   - Message [@BotFather](https://t.me/BotFather) on Telegram
   - `/newbot`, follow the prompts, copy the token it gives you

3. **Find your chat id** (needed so the bot can push you alerts)
   - Message [@userinfobot](https://t.me/userinfobot), it replies with your id

4. **Configure**
   ```bash
   cp .env.example .env
   # edit .env: paste BOT_TOKEN and CHAT_ID
   ```

5. **Run**
   ```bash
   python bot.py
   ```
   Open Telegram, find your bot, send `/start`.

## Commands

| Command | Example | What it does |
|---|---|---|
| `/buy CODE QTY PRICE` | `/buy BRACBANK 100 45.5` | Log a buy |
| `/sell CODE QTY PRICE` | `/sell BRACBANK 50 48.0` | Log a sell |
| `/portfolio` | | Holdings with live LTP and unrealized P/L |
| `/price CODE` | `/price GP` | Live price for one ticker |
| `/alert CODE above\|below PRICE` | `/alert BRACBANK above 50` | Set a price alert |
| `/alerts` | | List active alerts |
| `/report` | | Realized + unrealized P/L summary |
| `/history [CODE]` | `/history BRACBANK` | Recent transactions and dividends |
| `/dividends [CODE]` | `/dividends BRACBANK` | Dividends received (cash + bonus shares), with an ➕ Add button |
| `/dividend CODE cash AMOUNT\|PCT% [QTY]` | `/dividend BRACBANK cash 1.5` | Log a cash dividend: ৳1.5 per share |
| | `/dividend BRACBANK cash 15%` | …or 15% of face value (৳10 → ৳1.5/share) |
| `/dividend CODE stock PCT% [QTY]` | `/dividend BRACBANK stock 10%` | Log a stock dividend: 1 bonus share per 10 held |

The **💵 Dividends** menu button does the same thing with a guided flow
(pick a holding → cash or stock → enter the value).

### How dividends are counted

- `QTY` is optional and defaults to the shares you hold right now. Pass it
  if you held a different number on the record date, or no longer hold the
  stock.
- **Cash** dividends don't change your holdings or cost basis. They show up
  in `/report` (*Dividends received*, and *Total return* = realized +
  unrealized + dividends) and under `/portfolio`.
- **Stock** dividends add whole bonus shares at ৳0 cost, so your average
  cost per share drops (cost is unchanged, spread over more shares). The
  bonus is rounded down to whole shares.
- Enter the amount you actually received. If tax is deducted at source, use
  the after-tax per-share figure.
- Face value defaults to ৳10; set `DSE_FACE_VALUE` in `.env` to change it.

## Running it 24/7

For alerts to actually fire while you're away, the bot process needs to
stay running. On a Linux box (a small VPS, or a Pi at home) the simplest
option is a systemd service:

```ini
# /etc/systemd/system/dse-bot.service
[Unit]
Description=DSE Telegram Portfolio Bot
After=network.target

[Service]
WorkingDirectory=/path/to/dse-tracker
ExecStart=/usr/bin/python3 bot.py
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now dse-bot
```

## Notes & limitations

- **Single user by design.** `CHAT_ID` in `.env` is who alerts get pushed
  to; the bot doesn't separate multiple users' portfolios. If you want
  multi-user support later, that's a moderate change (key the JSON files
  by `update.effective_chat.id`).
- **Scraping is inherently fragile.** If dsebd.org changes its page
  layout, `dse_client.py`'s table parsing will need a small update — it
  raises a clear error rather than failing silently if that happens.
- **Not investment advice** — this is a personal tracking tool, prices
  can lag the live market slightly.
- **Backups:** `data/portfolio.json` is your only record of transactions.
  Back it up periodically (a cron `cp` to another disk, or commit it to a
  private git repo, is plenty for personal use).

## Possible next steps

- `/export` command to dump transactions as CSV
- Daily auto-summary pushed every morning (another `job_queue.run_daily`)
- Move from JSON files to SQLite if the transaction log grows large
