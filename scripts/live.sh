#!/usr/bin/env bash
# Kjør Olympus synlig i Kitty på en Hyprland-workspace og styr det med kitty @.
#   live.sh start [olympus-args…]   åpne Olympus på workspace $OLYMPUS_LIVE_WS (standard 2)
#   live.sh send "tekst"            skriv tekst og trykk Enter
#   live.sh keys ctrl+c             send tastetrykk
#   live.sh screen                  skriv ut det som står på skjermen nå
#   live.sh wait "mønster" [sek]    vent til mønsteret står på skjermen
#   live.sh stop                    lukk vinduet
set -euo pipefail
WS="${OLYMPUS_LIVE_WS:-2}"
SOCK="unix:${XDG_RUNTIME_DIR:-/tmp}/olympus-live.sock"
k() { kitty @ --to "$SOCK" "$@"; }

case "${1:-}" in
  start)
    shift
    k ls >/dev/null 2>&1 && { echo "kjører allerede"; exit 0; }
    rm -f "${SOCK#unix:}"
    # Hyprland med Lua-config: dispatch tar et Lua-uttrykk.
    hyprctl dispatch "hl.dsp.exec_cmd('kitty --hold --class olympus-live -o allow_remote_control=socket-only --listen-on $SOCK -d $HOME env PATH=$HOME/.local/bin:$PATH olympus $*', { workspace = '$WS silent' })" >/dev/null
    for _ in $(seq 50); do k ls >/dev/null 2>&1 && break; sleep 0.1; done
    echo "Olympus kjører på workspace $WS"
    ;;
  send) k send-text --match all "$2"$'\r' ;;
  keys) shift; k send-key --match all "$@" ;;
  screen) k get-text --match all ;;
  wait)
    pat="$2"; t="${3:-120}"
    for _ in $(seq $((t * 4))); do
      screen=$(k get-text --match all 2>/dev/null) || { echo "Olympus-vinduet er borte (krasj?)" >&2; exit 1; }
      grep -q -- "$pat" <<<"$screen" && exit 0
      sleep 0.25
    done
    echo "tidsavbrudd: fant ikke «$pat»" >&2; exit 1
    ;;
  stop) k close-window --match all 2>/dev/null || true ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
