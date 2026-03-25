#!/bin/bash
# Install launchd jobs for signal-capture daemon and health checks.

PLIST_DIR="$HOME/Library/LaunchAgents"
SL_BIN=$(which sl)
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$SL_BIN" ]; then
    echo "Error: 'sl' not found on PATH. Run 'pip install -e .' first."
    exit 1
fi

# Unload old jobs if they exist
launchctl unload "$PLIST_DIR/com.mannat.signal-capture.plist" 2>/dev/null
launchctl unload "$PLIST_DIR/com.mannat.signal-capture-health.plist" 2>/dev/null

mkdir -p "$PLIST_DIR"

# --- Persistent daemon: stays alive, restarts on crash ---
cat > "$PLIST_DIR/com.mannat.signal-capture.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mannat.signal-capture</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SL_BIN</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StandardOutPath</key>
    <string>$HOME/.signal-capture.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.signal-capture.log</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

# --- Health check: every 30 minutes ---
cat > "$PLIST_DIR/com.mannat.signal-capture-health.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mannat.signal-capture-health</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SL_BIN</string>
        <string>health</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl load "$PLIST_DIR/com.mannat.signal-capture.plist"
launchctl load "$PLIST_DIR/com.mannat.signal-capture-health.plist"

echo "Installed and loaded:"
echo "  com.mannat.signal-capture        (persistent daemon, auto-restart)"
echo "  com.mannat.signal-capture-health  (health check every 30 min)"
