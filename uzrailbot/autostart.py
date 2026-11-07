"""Start the app when the user logs in (Windows Run key, Linux autostart entry, macOS LaunchAgent)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

NAME = "UzRailBot"


def _python() -> str:
    exe = Path(sys.executable)
    if os.name == "nt":
        pyw = exe.with_name("pythonw.exe")      # no console window
        if pyw.exists():
            exe = pyw
    return str(exe)


def command() -> str:
    return f'"{_python()}" -m uzrailbot --background'


def _linux_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "uzrailbot.desktop"


def _mac_file() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "uz.uzrailbot.plist"


def is_enabled() -> bool:
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, NAME)
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _mac_file().exists()
    return _linux_file().exists()


def set_enabled(on: bool):
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(k, NAME)
                except OSError:
                    pass
        return
    if sys.platform == "darwin":
        f = _mac_file()
        if on:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>uz.uzrailbot</string>
<key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>uzrailbot</string><string>--background</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
""")
        else:
            f.unlink(missing_ok=True)
        return
    f = _linux_file()
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"""[Desktop Entry]
Type=Application
Name=UzRail Bot
Comment=Watches Uzbekistan Railways tickets
Exec={command()}
X-GNOME-Autostart-enabled=true
""")
    else:
        f.unlink(missing_ok=True)
