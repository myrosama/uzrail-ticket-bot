#!/usr/bin/env bash
# UzRail Bot installer for macOS and Linux.
#   curl -fsSL https://raw.githubusercontent.com/myrosama/uzrail-ticket-bot/main/install.sh | bash
# Desktop: installs the app with its window and tray / menu-bar icon, adds it to Applications (macOS) or the
#          app menu (Linux), and opens it. It starts by itself when you log in (Settings can turn that off).
# Server, Linux (no screen):  ... | bash -s -- --headless   sets up in the terminal, runs as a systemd user service.
# Uninstall:                  ... | bash -s -- --uninstall
# Run it again to update (the app also updates itself).
set -euo pipefail

REPO="myrosama/uzrail-ticket-bot"
OS="$(uname -s)"
MAC=0
if [ "$OS" = Darwin ]; then MAC=1; fi
if [ "$MAC" = 1 ]; then
  HOME_DIR="$HOME/Library/Application Support/UzRailBot"      # where the app keeps everything on a Mac
else
  HOME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/UzRailBot"
fi
APP_DIR="$HOME_DIR/app"
VENV="$HOME_DIR/venv"
BIN="$HOME/.local/bin/uzrailbot"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/uzrailbot.desktop"
AUTOSTART_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/uzrailbot.desktop"
SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/uzrailbot.service"
MAC_APP="$HOME/Applications/UzRail Bot.app"
MAC_AGENT="$HOME/Library/LaunchAgents/uz.uzrailbot.plist"
SOURCE="${UZRAILBOT_SOURCE:-}"
START=1

MODE=desktop
if [ "$MAC" = 0 ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then MODE=headless; fi
for arg in "$@"; do
  case "$arg" in
    --headless) MODE=headless ;;
    --desktop) MODE=desktop ;;
    --uninstall) MODE=uninstall ;;
    --no-start) START=0 ;;
    --source=*) SOURCE="${arg#--source=}" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then SUDO="sudo"; fi
fi

stop_app() {
  if have systemctl && systemctl --user is-active --quiet uzrailbot 2>/dev/null; then
    systemctl --user stop uzrailbot || true
  fi
  pkill -f "$VENV/bin/python.* -m uzrailbot" 2>/dev/null || true
}

if [ "$MODE" = uninstall ]; then
  step "Removing UzRail Bot"
  stop_app
  if have systemctl; then systemctl --user disable uzrailbot 2>/dev/null || true; fi
  if [ "$MAC" = 1 ] && [ -f "$MAC_AGENT" ]; then launchctl unload "$MAC_AGENT" 2>/dev/null || true; fi
  rm -rf "$APP_DIR" "$VENV" "$MAC_APP"
  rm -f "$BIN" "$DESKTOP_FILE" "$AUTOSTART_FILE" "$SERVICE_FILE" "$MAC_AGENT"
  say "Removed the app. Your settings and logs are still in $HOME_DIR (delete it to remove them too)."
  exit 0
fi

printf '\n  \033[32mUzRail Bot installer\033[0m (%s, %s mode)\n' "$([ "$MAC" = 1 ] && echo macOS || echo Linux)" "$MODE"

# ---------------------------------------------------------------- 1. Python (and, on Linux, the window libraries)
step "Checking Python"
PY=python3
if [ "$MAC" = 1 ]; then          # prefer Homebrew's Python over the system one
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi
python_ok() { "$PY" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; }
gui_ok() { "$PY" -c 'import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")' 2>/dev/null \
        || "$PY" -c 'import gi; gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.0")' 2>/dev/null; }

if [ "$MAC" = 1 ]; then
  # The window and menu-bar icon come from pip (pyobjc): only Python 3.9+ is needed.
  if ! python_ok; then
    if have brew; then
      say "Installing Python with Homebrew (one time)..."
      brew install python@3.12
      for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$cand" ]; then PY="$cand"; break; fi
      done
    fi
  fi
  if ! python_ok; then
    echo "Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/macos/ (or Homebrew:" >&2
    echo "brew install python), then run this line again." >&2
    exit 1
  fi
else
  need_pkgs=0
  python_ok || need_pkgs=1
  have curl || have wget || need_pkgs=1
  if [ "$MODE" = desktop ] && ! gui_ok; then need_pkgs=1; fi
  if [ "$need_pkgs" = 1 ]; then
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
      echo "Some system packages are missing and sudo is not available. Install Python 3.9+ (with venv)" >&2
      echo "and, for the window, GTK + WebKit2 Python bindings, then run this again." >&2
      exit 1
    fi
    say "Installing what's missing (you may be asked for your password)..."
    if have apt-get; then
      pkgs="python3 python3-venv python3-pip curl"
      if [ "$MODE" = desktop ]; then
        pkgs="$pkgs python3-gi python3-gi-cairo gir1.2-gtk-3.0"
        if apt-cache show gir1.2-webkit2-4.1 >/dev/null 2>&1; then pkgs="$pkgs gir1.2-webkit2-4.1"; else pkgs="$pkgs gir1.2-webkit2-4.0"; fi
        if apt-cache show gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then pkgs="$pkgs gir1.2-ayatanaappindicator3-0.1"; fi
      fi
      $SUDO apt-get update -qq
      # shellcheck disable=SC2086
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs
    elif have dnf; then
      pkgs="python3 python3-pip curl"
      [ "$MODE" = desktop ] && pkgs="$pkgs python3-gobject gtk3 webkit2gtk4.1 libayatana-appindicator-gtk3"
      # shellcheck disable=SC2086
      $SUDO dnf install -y -q $pkgs || $SUDO dnf install -y -q ${pkgs/webkit2gtk4.1/webkit2gtk3}
    elif have pacman; then
      pkgs="python curl"
      [ "$MODE" = desktop ] && pkgs="$pkgs python-gobject gtk3 webkit2gtk-4.1 libayatana-appindicator"
      # shellcheck disable=SC2086
      $SUDO pacman -S --needed --noconfirm $pkgs
    elif have zypper; then
      pkgs="python3 python3-pip curl"
      [ "$MODE" = desktop ] && pkgs="$pkgs python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-WebKit2-4_1"
      # shellcheck disable=SC2086
      $SUDO zypper -q install -y $pkgs
    else
      echo "Unknown package manager. Install Python 3.9+ with venv (and GTK/WebKit2 bindings for the window)." >&2
      exit 1
    fi
  fi
  python_ok || { echo "Python 3.9+ with the venv module is required." >&2; exit 1; }
  if [ "$MODE" = desktop ] && ! gui_ok; then
    say "The window library (WebKit2GTK) is not available; the status page will open in your browser instead."
  fi
fi
say "Using $("$PY" --version)"

# ---------------------------------------------------------------- 2. app files
step "Getting UzRail Bot"
stop_app
mkdir -p "$HOME_DIR"
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
if [ -n "$SOURCE" ]; then
  src="$(cd "$SOURCE" && pwd)"
  say "From local folder $src"
else
  url="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
  if have curl; then curl -fsSL "$url" -o "$staging/app.tgz"; else wget -qO "$staging/app.tgz" "$url"; fi
  tar -xzf "$staging/app.tgz" -C "$staging"
  src="$(find "$staging" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  say "Downloaded the latest version from github.com/$REPO"
fi
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
for item in uzrailbot pyproject.toml requirements.txt README.md LICENSE; do
  if [ -e "$src/$item" ]; then cp -R "$src/$item" "$APP_DIR/"; fi
done
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------- 3. private Python environment
step "Installing (this takes a minute or two the first time)"
if [ ! -x "$VENV/bin/python" ]; then
  if [ "$MAC" = 1 ]; then
    "$PY" -m venv "$VENV"
  else
    # system site packages: the window and tray use the distro's GTK/WebKit Python bindings
    "$PY" -m venv --system-site-packages "$VENV"
  fi
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$APP_DIR"
say "Installed version $("$VENV/bin/python" -c 'import uzrailbot; print(uzrailbot.__version__)')"

mkdir -p "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
exec "$VENV/bin/python" -m uzrailbot "\$@"
EOF
chmod +x "$BIN"
say "Command: uzrailbot  (uzrailbot report | live | where)"

# ---------------------------------------------------------------- 4. desktop, Mac or server
if [ "$MAC" = 1 ]; then
  step "Adding UzRail Bot to your Applications"
  rm -rf "$MAC_APP"
  mkdir -p "$MAC_APP/Contents/MacOS" "$MAC_APP/Contents/Resources"
  cat > "$MAC_APP/Contents/MacOS/UzRailBot" <<EOF
#!/bin/bash
exec "$VENV/bin/python" -m uzrailbot "\$@"
EOF
  chmod +x "$MAC_APP/Contents/MacOS/UzRailBot"
  icon_line=""
  if have sips && have iconutil; then              # the train icon, as a proper .icns (best effort)
    set_dir="$staging/UzRailBot.iconset"
    mkdir -p "$set_dir"
    for sz in 16 32 64 128 256; do
      sips -z "$sz" "$sz" "$APP_DIR/uzrailbot/ui/icon.png" --out "$set_dir/icon_${sz}x${sz}.png" >/dev/null 2>&1 || true
    done
    if iconutil -c icns "$set_dir" -o "$MAC_APP/Contents/Resources/UzRailBot.icns" 2>/dev/null; then
      icon_line="<key>CFBundleIconFile</key><string>UzRailBot</string>"
    fi
  fi
  cat > "$MAC_APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>UzRail Bot</string>
<key>CFBundleDisplayName</key><string>UzRail Bot</string>
<key>CFBundleIdentifier</key><string>uz.uzrailbot.app</string>
<key>CFBundleExecutable</key><string>UzRailBot</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSMinimumSystemVersion</key><string>10.13</string>
$icon_line
</dict></plist>
EOF
  say "Applications: UzRail Bot (open it from Launchpad or Spotlight)"
  printf '\n  \033[32mDone!\033[0m\n'
  if [ "$START" = 1 ]; then
    open "$MAC_APP" 2>/dev/null || nohup "$VENV/bin/python" -m uzrailbot </dev/null >/dev/null 2>&1 &
    say "UzRail Bot is opening. Paste your Telegram bot token there to finish setup."
    say "It also starts by itself when you log in (turn that off in Settings)."
  fi
elif [ "$MODE" = desktop ]; then
  step "Adding to the app menu"
  mkdir -p "$(dirname "$DESKTOP_FILE")"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=UzRail Bot
Comment=Books Uzbekistan Railways tickets the moment they appear
Exec=$VENV/bin/python -m uzrailbot
Icon=$APP_DIR/uzrailbot/ui/icon.png
Terminal=false
Categories=Utility;Network;
EOF
  printf '\n  \033[32mDone!\033[0m\n'
  if [ "$START" = 1 ]; then
    nohup "$VENV/bin/python" -m uzrailbot </dev/null >/dev/null 2>&1 &
    say "UzRail Bot is opening. Paste your Telegram bot token there to finish setup."
    say "It also starts by itself when you log in (turn that off in Settings)."
  fi
elif [ "$START" = 1 ]; then
  step "Setting up (server mode)"
  if [ ! -s "$HOME_DIR/data/config.json" ] || ! "$VENV/bin/python" - <<'EOF'
import sys
from uzrailbot.store import Store
sys.exit(0 if Store().is_configured() else 1)
EOF
  then
    if [ -r /dev/tty ]; then
      "$VENV/bin/python" -m uzrailbot setup </dev/tty
    else
      say "Run 'uzrailbot setup' in a terminal to link your Telegram bot, then: systemctl --user restart uzrailbot"
    fi
  fi
  if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$(dirname "$SERVICE_FILE")"
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=UzRail Bot (train ticket watcher)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$VENV/bin/python -m uzrailbot run
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now uzrailbot
    # keep running after you log out
    loginctl enable-linger "$(id -un)" 2>/dev/null || $SUDO loginctl enable-linger "$(id -un)" 2>/dev/null || true
    printf '\n  \033[32mDone!\033[0m The bot runs in the background and restarts by itself.\n'
    say "Logs: journalctl --user -u uzrailbot -f     Stop: systemctl --user stop uzrailbot"
  else
    printf '\n  \033[32mDone!\033[0m\n'
    say "Start it with: uzrailbot run   (systemd is not available here, so keep that terminal open, e.g. in tmux)"
  fi
else
  printf '\n  \033[32mDone!\033[0m (not started: --no-start)\n'
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "Tip: add ~/.local/bin to your PATH to use the 'uzrailbot' command." ;; esac
echo
