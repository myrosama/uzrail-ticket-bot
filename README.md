<p align="center"><img src="docs/icon.png" width="96" alt=""></p>

# UzRail Ticket Bot

Watches **eticket.uzrailpass.uz** (Uzbekistan Railways) from your own computer and books train seats the
moment they appear: sold-out trains, returned tickets, seats added at random times. You control it from your
own Telegram bot. Pick the route, dates, classes (plaskart, kupe, SV, sitting), passengers and a maximum
price. When seats are held you get a **Pay** button (Hamkorbank's own payment page). After you pay, the
ticket PDF arrives in the chat.

> **Unofficial.** It sends the same requests as the railway website, using your own account. Automated
> booking may be against the site's terms, so use it at your own risk. It never pays by itself: you always
> confirm the payment on the bank's page.

## Install

**Windows**: open PowerShell and paste:

```powershell
irm https://raw.githubusercontent.com/myrosama/uzrail-ticket-bot/main/install.ps1 | iex
```

**Linux** (Ubuntu, Debian, Fedora, Arch…):

```bash
curl -fsSL https://raw.githubusercontent.com/myrosama/uzrail-ticket-bot/main/install.sh | bash
```

On a server without a screen, add `-s -- --headless`. Setup then runs in the terminal, and the bot runs as a
systemd user service.

The installer gets Python if needed, installs the app into a private folder, adds it to the Start menu or app
menu, and opens it. Run the same line again to update.

## First run (2 minutes)

1. **Create a Telegram bot.** Open [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. **Paste the token** into the UzRail Bot window. Then open your bot on your phone and press **Start**. The
   window shows your name, and you confirm. Only your Telegram account can control the bot.
3. **In Telegram, tap 🚆 Trips & settings.** A small app opens inside Telegram. There you:
   - connect your eticket.uzrailpass.uz login;
   - add a trip: from/to, one or more dates, accepted classes, max price, passengers in priority order (up to
     4, from the saved passengers on your railway account), and **Notify** or **Auto-buy**.
4. Leave the computer on. On Windows the bot stops it from going to sleep while trips are being watched.

## What you get in Telegram

1. **🎫 Tickets appeared**: the train, class, seats and price.
2. **✅ Seats reserved**, with car, seats and passengers. If someone else was faster, you get **❌ Not this
   time** and the bot keeps watching.
3. **💳 Pay** button with a countdown. The railway holds seats for about 12 minutes. If you haven't paid by
   then, the bot re-reserves up to 3 times. After payment it sends the **ticket PDF**.

## The desktop app

A small window plus a tray icon: **green** means watching, **amber** means seats are held and waiting for
payment, **grey** means paused, and **red** means there's a problem. Closing the window keeps the bot running in
the tray.

| Page | |
| --- | --- |
| Status | What it's doing, last check, checks per minute, response time; held seats with Pay / Cancel |
| Trips | Your trips, pause/resume each |
| Activity | Every detection and booking step, in plain words |
| Settings | Start with the computer, Telegram link, data folder, Quit |

Command line (`uzrailbot` on Linux; on Windows `%LOCALAPPDATA%\UzRailBot\venv\Scripts\python -m uzrailbot`):

| Command | |
| --- | --- |
| `uzrailbot` | The desktop app |
| `uzrailbot run` | No window: console mode for servers |
| `uzrailbot setup` | Link a different Telegram bot (in the terminal) |
| `uzrailbot report [N]` | Timeline of the latest session: every check, detection and booking step with timings |
| `uzrailbot live` | Live console view of what the bot is doing |
| `uzrailbot autostart on\|off` | Start with the computer |
| `uzrailbot where` | Where settings and logs are kept |

## How it works, and how fast

There is no browser. The bot talks to the railway's API directly (see [docs/RESEARCH.md](docs/RESEARCH.md)):

- **Watching:** about one train-list request every 0.8 s per route and date (faster if you choose). The site
  answers in about 40–120 ms.
- **Booking:** the moment seats appear, one reserve request goes to the train's most likely car on an already
  open connection. Other known cars of that train are tried in parallel. The railway picks the seats
  (`1-99`), so there is no seat-map round trip. The railway confirms the hold 1–2 s later over its live
  channel. The bot keeps the first hold and releases any duplicates.
- **Measured on a real sold-out train:** 3.3 s and 2.8 s from "seat visible on the site" to "seat held in
  your name". The 2.8 s one was a seat someone else had just cancelled. The parts: about 0.8 s to notice
  (the check interval), under 0.1 s to send, and 1–2 s for the railway to confirm.
- **Polite:** the bot uses a request budget well below the site's limits. On a "slow down" answer it backs
  off for 30 s.

## Updates

The app updates itself:

- **When:** when it opens, and every 6 hours while it runs.
- **How:** it checks GitHub for a newer version, then installs it in the background while the old version
  keeps watching. When that's done, it restarts into the new version.
- **Never mid-booking:** it won't restart while a booking is in progress or seats are held for you. The
  restart waits until that's over.
- **Telegram:** the bot tells you "🔄 Updated to vX".

To turn automatic updates off, go to Settings → *Update automatically*. The app then only tells you about new
versions, and you install them with **Update now**. Running the install line again also updates.

## Privacy

Everything stays on your computer: `%LOCALAPPDATA%\UzRailBot` on Windows, `~/.local/share/UzRailBot` on Linux.

- The railway password and bot token are encrypted at rest. On Windows this uses DPAPI, tied to your Windows
  account. On Linux it uses a key file only you can read.
- The Telegram mini app is a static page (`docs/` on GitHub Pages) with no server behind it. It encrypts your
  railway password on your phone with your PC's public key, so Telegram only ever carries ciphertext.
- The status window talks to the bot over `127.0.0.1` with a random per-run key.
- Session recordings (`logs/sessions`) never contain passport numbers.

## Uninstall

Windows (PowerShell):
`& ([scriptblock]::Create((irm https://raw.githubusercontent.com/myrosama/uzrail-ticket-bot/main/install.ps1))) -Uninstall`

Linux: `curl -fsSL https://raw.githubusercontent.com/myrosama/uzrail-ticket-bot/main/install.sh | bash -s -- --uninstall`

Your settings folder is kept; delete it to remove everything.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .     # Windows: start.bat
python -m uzrailbot                                    # UZRAILBOT_HOME=<dir> keeps test data separate
python tools/build_stations.py                         # refresh docs/stations.json (station list for the mini app)
python tools/make_icons.py                             # regenerate the icons
```

`uzrailbot/` is the bot:
- `engine.py`: watch, book, hold, renew.
- `eticket.py`: the railway API client.
- `telegram.py`: the bot.
- `gui.py` and `ui/`: the desktop window and tray.

`docs/` is the Telegram mini app (served by GitHub Pages).

To ship an update to every installed copy, raise `__version__` in `uzrailbot/__init__.py` and push to `main`.
Pushes that don't change the version don't trigger updates. The mini app (`docs/`) updates for everyone as
soon as GitHub Pages publishes it.

MIT licence.
