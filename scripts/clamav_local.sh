#!/usr/bin/env bash
# ClamAV uten root: henter Arch-pakkene (clamav, libmspack, libmilter), sjekker signaturene med pacman-key og pakker
# dem ut i ~/.local/opt/clamav. Virusdatabasen ligger i ~/.olympus/clamav/db og oppdateres av olympus-freshclam.timer.
# Bruk: scripts/clamav_local.sh            (installer/oppdater programmet + databasen)
#       scripts/clamav_local.sh update     (bare databasen – det timeren kjører)
set -euo pipefail
O="$HOME/.local/opt/clamav"
H="$HOME/.olympus/clamav"
export LD_LIBRARY_PATH="$O/usr/lib" CVD_CERTS_DIR="$O/etc/clamav/certs"
mkdir -p "$H/db"
[ -f "$H/freshclam.conf" ] || cat > "$H/freshclam.conf" <<CONF
DatabaseDirectory $H/db
UpdateLogFile $H/freshclam.log
DatabaseMirror database.clamav.net
Checks 1
CONF
if [ "${1:-}" != update ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  for p in clamav libmspack libmilter; do
    info=$(pacman -Si "$p")
    ver=$(awk '/^Version/{print $3}' <<<"$info"); repo=$(awk '/^Repository/{print $3}' <<<"$info")
    f="$p-$ver-x86_64.pkg.tar.zst"
    curl -sfL -o "$tmp/$f" "https://geo.mirror.pkgbuild.com/$repo/os/x86_64/$f"
    curl -sfL -o "$tmp/$f.sig" "https://geo.mirror.pkgbuild.com/$repo/os/x86_64/$f.sig"
    pacman-key --verify "$tmp/$f.sig" "$tmp/$f" 2>&1 | grep -q "Good signature" || { echo "Ugyldig signatur: $f"; exit 1; }
    mkdir -p "$O"
    tar --zstd -xf "$tmp/$f" -C "$O" usr $( [ "$p" = clamav ] && echo etc )
  done
fi
nice -n 15 "$O/usr/bin/freshclam" --config-file="$H/freshclam.conf" --quiet
echo "ClamAV $("$O/usr/bin/clamscan" --version | cut -d/ -f1 | cut -d' ' -f2) · database: $(du -sh "$H/db" | cut -f1)"
