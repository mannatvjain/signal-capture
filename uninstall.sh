#!/bin/bash
# Remove launchd jobs for signal-capture.

PLIST_DIR="$HOME/Library/LaunchAgents"

launchctl unload "$PLIST_DIR/com.mannat.signal-capture.plist" 2>/dev/null
launchctl unload "$PLIST_DIR/com.mannat.signal-capture-health.plist" 2>/dev/null
rm -f "$PLIST_DIR/com.mannat.signal-capture.plist"
rm -f "$PLIST_DIR/com.mannat.signal-capture-health.plist"

echo "Unloaded and removed signal-capture launchd jobs."
