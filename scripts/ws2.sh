#!/usr/bin/env bash
# Workspace 2 = Olympus: Lab's/Zeus-chatten, `olympus zeus` (se og styr Zeus) og `olympus status`, alltid.
# Startes av Hyprland (autostart.lua). Åpner vinduene som mangler hvert 5. sekund – lukker du ett, kommer det
# tilbake. Stopp for økten med:  pkill -f olympus/scripts/ws2.sh
set -u
WS="${OLYMPUS_WS:-2}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PATH="$HOME/.local/bin:$PATH"
have() { hyprctl clients -j 2>/dev/null | grep -q "\"class\": \"$1\""; }
launch() {  # $1 = klasse, resten = kommando
    local class="$1"; shift
    hyprctl dispatch "hl.dsp.exec_cmd('kitty --class $class -d $HOME $*', { workspace = '$WS silent' })" >/dev/null
}
while true; do
    # Olympus-TUI-en (uten argumenter) kan også være en du har åpnet selv – da startes ingen ny.
    if ! pgrep -f 'bin/olympus$' >/dev/null && ! have olympus-live; then OLYMPUS_LIVE_WS="$WS" "$DIR/live.sh" start >/dev/null 2>&1; sleep 2; fi
    if ! have olympus-status; then launch olympus-status olympus status; sleep 2; fi
    if ! have olympus-zeus; then launch olympus-zeus olympus zeus; sleep 2; fi
    sleep 5
done
