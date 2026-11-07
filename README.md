# UzRail Ticket Bot

Watches **eticket.uzrailpass.uz** (Uzbekistan Railways) from your own PC and books train seats the moment they
appear. You control it from your own Telegram bot: set up trips in the chat, get a **Pay** button when seats are
held (Hamkorbank's own payment page, 12 minutes to pay), and receive the ticket PDF after paying.

> Unofficial tool. It uses the same requests as the railway's website, with your own account. Automated
> buying may be against the site's terms; use it at your own risk. It never pays by itself.

## Quick start (Windows)

1. Create a Telegram bot: open **@BotFather**, send `/newbot`, copy the token.
2. Double-click **`start.bat`**. The first run installs what it needs, then asks for the bot token and tells you to
   press **Start** in your bot. Optionally it asks for your eticket.uzrailpass.uz login (or send `/account` to
   the bot later).
3. In Telegram tap **➕ New trip**: stations, dates, ticket class, maximum price, *notify* or *auto-buy*, passengers
   (from your saved passengers on the railway site), check speed.

Keep the PC on and awake while it watches.

## Commands

| In Telegram | What it does |
| --- | --- |
| ➕ New trip / `/new` | Guided trip setup |
| 🗂 My trips / `/trips` | List, pause, resume, delete trips |
| 📊 Status / `/status` | What the bot is doing right now |
| `/account` | Railway site login (your password message is deleted at once) |
| ⏸ Pause / ▶️ Resume | Stop or restart everything |

| On the PC | What it does |
| --- | --- |
| `start.bat` | Run the bot (first run: setup) |
| `start.bat setup` | Link a different Telegram bot |
| `start.bat report` | Timeline of the latest session: every check, detection, booking step and its timing |
| `start.bat where` | Where settings and logs are kept |

## How fast is it

Measured against the live site (details in `docs/RESEARCH.md`):

- A freed seat shows up in the railway's train list about **1.8 s** after it is freed, for everyone.
- The bot checks every 0.8 s (up to every 0.2 s), books with one request (~80 ms), and the railway confirms the
  hold about 1.1-2 s later. Typical "seat appeared → held": **1.7-2.3 s**.
- When seats *just* appeared, it reserves directly on the train's known cars at once, skipping the car lookup
  whose 3-5 s cache can still show the seat as taken.

## Privacy

Everything stays on your PC: settings in `%LOCALAPPDATA%\UzRailBot`, the railway password and bot token
encrypted with Windows DPAPI. Session logs never contain passport numbers.
