#!/bin/sh
# Installs the PIP Farming bot on this Mac: Python check, folders, and the 3 launchd jobs
# (the control bot, the 30-min-early previews, the posting). Run again any time; it reloads everything.
set -e
cd "$(dirname "$0")"; HERE="$(pwd)"
PY="$(command -v python3)"; [ -n "$PY" ] || { echo "Install Python 3 first: brew install python"; exit 1; }
[ -f .env ] || { echo "Missing .env. Copy .env.example to .env and fill it in."; exit 1; }
[ -d "/Applications/Google Chrome.app" ] || echo "Warning: Google Chrome is needed to draw the images."
command -v claude >/dev/null || [ -x "$HOME/.local/node/bin/claude" ] || echo "Warning: Claude Code CLI not found (npm i -g @anthropic-ai/claude-code, then run: claude, and log in)."
mkdir -p state logs posts
LA="$HOME/Library/LaunchAgents"; mkdir -p "$LA"
plist() {   # name, args, schedule-or-keepalive
cat > "$LA/com.pipfarming.$1.plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.pipfarming.$1</string>
<key>ProgramArguments</key><array><string>$PY</string>$2</array>
<key>WorkingDirectory</key><string>$HERE</string>
$3
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/node/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string><key>LANG</key><string>en_US.UTF-8</string><key>TZ</key><string>Asia/Bangkok</string></dict>
<key>StandardOutPath</key><string>$HERE/logs/$1.log</string><key>StandardErrorPath</key><string>$HERE/logs/$1.log</string>
</dict></plist>
P
launchctl bootout "gui/$(id -u)/com.pipfarming.$1" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LA/com.pipfarming.$1.plist"
echo "loaded com.pipfarming.$1"
}
at() { printf '<key>StartCalendarInterval</key><array>'; for t in "$@"; do printf '<dict><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>' "${t%:*}" "${t#*:}"; done; printf '</array>'; }
plist bot  "<string>$HERE/bot.py</string>" '<key>KeepAlive</key><true/><key>RunAtLoad</key><true/>'
plist prep "<string>$HERE/autopost.py</string><string>prep</string>" "$(at 8:30 12:30 16:30 19:30)"
plist post "<string>$HERE/autopost.py</string><string>post</string>" "$(at 9:00 13:00 17:00 20:00)"
./data/fetch-providers.sh || echo "Provider data not fetched yet: check PIP_SSH_KEY in .env and the key file."
echo "Done. Open the bot on Telegram and send /start."
