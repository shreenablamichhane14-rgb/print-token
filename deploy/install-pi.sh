#!/usr/bin/env bash
# Installs or updates Print Token on a Raspberry Pi (Raspberry Pi OS Trixie, Python 3.12 or newer).
#
#   sudo bash deploy/install-pi.sh
#
# Safe to run again. It updates the code in /opt/print-token and the service, and never touches
# what is already in /etc/print-token (settings, secrets) or /var/lib/print-token (database, log,
# dry run output). The printer stays whatever config.toml says; a fresh install says DRYRUN.
#
# Layout it creates:
#   /opt/print-token            the code from GitHub and its Python environment, owned by root
#   /etc/print-token/config.toml   settings (no secrets)
#   /etc/print-token/secrets/env   secrets: bot token, allowed ids, PIN hash. The folder belongs to
#                               the service account, because setpin rewrites the file in place.
#   /var/lib/print-token        database, log, dry run PDFs
#   user printtoken             the account the gateway runs as; it cannot log in
#   print-token.service         the gateway: starts on boot, comes back after any exit
#   print-token-wake.timer      a wake-up alarm, so a Pi that is off but plugged in comes back on
set -euo pipefail

REPO=https://github.com/shreenablamichhane14-rgb/print-token.git
APP=/opt/print-token
ETC=/etc/print-token
ENVFILE=$ETC/secrets/env
STATE=/var/lib/print-token
SVC_USER=printtoken
UNIT=print-token.service

say() { printf '\n== %s\n' "$*"; }
die() { printf 'install-pi: %s\n' "$*" >&2; exit 1; }

# Everything lives in main() so the whole file is read before any of it runs: the update below
# may replace this very script.
main() {
    local here
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    [ "$(id -u)" -eq 0 ] || die "run it with sudo"

    say "Checking what the Pi already has"
    local missing=""
    for tool in git python3 lp lpstat; do
        command -v "$tool" >/dev/null || missing="$missing $tool"
    done
    [ -z "$missing" ] || die "missing:$missing (sudo apt install git python3-venv cups)"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
        || die "Python 3.12 or newer is needed, this is $(python3 --version)"
    python3 -c 'import venv, ensurepip' 2>/dev/null || die "missing: python3-venv (sudo apt install python3-venv)"

    say "Service account and folders"
    if ! id "$SVC_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    fi
    install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE"
    install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
    install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$ETC/secrets"

    say "Code"
    if [ -d "$APP/.git" ]; then
        git -C "$APP" pull --ff-only
    else
        git clone "$REPO" "$APP"
    fi
    echo "at commit $(git -C "$APP" log -1 --format='%h %s')"

    say "Python environment"
    [ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
    "$APP/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP/requirements.txt"
    # The service user cannot write here, so the bytecode is made now.
    "$APP/.venv/bin/python" -m compileall -q "$APP/printtoken"

    say "Settings"
    if [ ! -e "$ETC/config.toml" ]; then
        sed "s|^data_dir = .*|data_dir = \"$STATE\"|" "$APP/config.example.toml" > "$ETC/config.toml"
        echo "wrote $ETC/config.toml from config.example.toml (printer = DRYRUN, email off)"
    else
        echo "kept $ETC/config.toml"
    fi
    chown root:"$SVC_USER" "$ETC/config.toml"
    chmod 0640 "$ETC/config.toml"
    if [ ! -e "$ENVFILE" ]; then
        install -m 0600 -o "$SVC_USER" -g "$SVC_USER" /dev/null "$ENVFILE"
        echo "made an empty $ENVFILE"
    else
        echo "kept $ENVFILE"
    fi

    say "Service and command"
    # Before the deploy folder is in the repository this script is copied over by hand, so the
    # files next to it win; after that they are the same files.
    install -m 0644 "$here/print-token.service" "/etc/systemd/system/$UNIT"
    install -m 0644 "$here/print-token-wake.service" "$here/print-token-wake.timer" /etc/systemd/system/
    install -m 0755 "$here/print-token" /usr/local/bin/print-token
    systemctl daemon-reload
    systemctl enable "$UNIT" >/dev/null 2>&1
    systemctl enable print-token-wake.service >/dev/null 2>&1      # its run at the start of every boot
    systemctl enable --now print-token-wake.timer >/dev/null 2>&1
    echo "installed $UNIT (starts on boot), the wake-up alarm timer and /usr/local/bin/print-token"

    if grep -q '^PRINT_TOKEN_BOT_TOKEN=.' "$ENVFILE"; then
        systemctl restart "$UNIT"
        echo "gateway restarted: print-token status"
    else
        cat <<EOF

The secrets are not on this Pi yet, so the gateway is not started. Put these in $ENVFILE
(keep it owned by $SVC_USER, mode 600), one per line, then: sudo systemctl start print-token
  PRINT_TOKEN_BOT_TOKEN=...      the bot token from @BotFather
  PRINT_TOKEN_ALLOWED_IDS=...    your numeric Telegram id
  PRINT_TOKEN_NOTIFY_CHAT=...    the same id
EOF
    fi
}

main "$@"
