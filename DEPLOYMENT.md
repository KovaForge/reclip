# KovaForge Deployment

This fork is deployed locally on Michael's iMac as a per-user `launchd` service.

## Current service

- App path: `/Users/mike/Projects/KovaForge/reclip`
- Service label: `ai.kovaforge.reclip`
- LaunchAgent: `/Users/mike/Library/LaunchAgents/ai.kovaforge.reclip.plist`
- Bind address: `127.0.0.1:8899`
- Local URL: <http://127.0.0.1:8899>
- Logs:
  - `logs/reclip.out.log`
  - `logs/reclip.err.log`

It is intentionally bound to loopback for personal use. Do not expose this app publicly without adding authentication/rate limiting first; it can invoke `yt-dlp`/`ffmpeg` against arbitrary user-submitted URLs.

## Useful commands

```bash
# status
launchctl print gui/$(id -u)/ai.kovaforge.reclip

# restart
launchctl kickstart -k gui/$(id -u)/ai.kovaforge.reclip

# stop/unload
launchctl bootout gui/$(id -u) /Users/mike/Library/LaunchAgents/ai.kovaforge.reclip.plist

# smoke test
curl -fsS http://127.0.0.1:8899/ >/tmp/reclip-index.html
```

## Recreate service

```bash
cd /Users/mike/Projects/KovaForge/reclip
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
mkdir -p logs ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/ai.kovaforge.reclip.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.kovaforge.reclip</string>
  <key>WorkingDirectory</key><string>/Users/mike/Projects/KovaForge/reclip</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/mike/Projects/KovaForge/reclip/venv/bin/python</string>
    <string>/Users/mike/Projects/KovaForge/reclip/app.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOST</key><string>127.0.0.1</string>
    <key>PORT</key><string>8899</string>
    <key>PATH</key><string>/Users/mike/Projects/KovaForge/reclip/venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/mike/Projects/KovaForge/reclip/logs/reclip.out.log</string>
  <key>StandardErrorPath</key><string>/Users/mike/Projects/KovaForge/reclip/logs/reclip.err.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.kovaforge.reclip.plist
launchctl kickstart -k gui/$(id -u)/ai.kovaforge.reclip
```
