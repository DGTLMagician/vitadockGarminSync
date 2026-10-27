#!/usr/bin/env bash
# Install or update vitadock-garmin-sync as a systemd timer on Linux.
#
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/DGTLMagician/vitadockGarminSync/main/install.sh)"
#
# Re-running it updates the code and keeps the existing configuration and tokens.
# Not `curl ... | sudo bash`: then stdin is the pipe, and sudo (sudo-rs and
# sudo with use_pty) cannot pass keyboard input on to the prompts below.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/DGTLMagician/vitadockGarminSync.git}"
BRANCH="${BRANCH:-main}"
PREFIX=/opt/vitadock-garmin-sync
SRC="$PREFIX/src"
VENV="$PREFIX/.venv"
ENV_FILE=/etc/vitadock-garmin-sync.env
STATE=/var/lib/vitadock-garmin-sync
SVC_USER=vitadock-sync

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mfout:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "start als root (sudo)"
command -v systemctl >/dev/null || die "systemd is vereist"

# Only prompt when stdin is a terminal; never fall back to /dev/tty, which
# hangs under sudo's own pty when stdin is a pipe.
INTERACTIVE=""
[ -t 0 ] && INTERACTIVE=1
ONELINER='sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/DGTLMagician/vitadockGarminSync/main/install.sh)"'
ask() { # ask VAR "prompt"  (input hidden)
    [ -n "$INTERACTIVE" ] || die "$1 ontbreekt in $ENV_FILE. Start de installer interactief met:
  $ONELINER
of geef de waarde mee: sudo $1=... bash install.sh"
    local v
    read -rsp "$2: " v
    echo >&2
    printf '%s' "$v"
}

# Keep pip/uv caches out of the invoking user's home (sudo may keep $HOME).
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_CACHE_DIR="$PREFIX/.uv-cache"

install_pkgs() {
    if command -v apt-get >/dev/null; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
    elif command -v dnf >/dev/null; then dnf install -y -q "$@"
    elif command -v pacman >/dev/null; then pacman -Sy --noconfirm --needed "$@"
    elif command -v apk >/dev/null; then apk add -q "$@"
    elif command -v zypper >/dev/null; then zypper -q install -y "$@"
    else die "installeer handmatig: $*"; fi
}

say "Benodigdheden controleren"
missing=()
command -v git >/dev/null || missing+=(git)
command -v python3 >/dev/null || missing+=(python3)
command -v curl >/dev/null || missing+=(curl)
[ ${#missing[@]} -eq 0 ] || install_pkgs "${missing[@]}"
if command -v apt-get >/dev/null && ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
    install_pkgs python3-venv
fi

id "$SVC_USER" >/dev/null 2>&1 || {
    say "Systeemgebruiker $SVC_USER aanmaken"
    useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
        || useradd --system --home "$STATE" --shell /sbin/nologin "$SVC_USER"
}

say "Code ophalen ($REPO_URL, $BRANCH)"
install -d -m 755 "$PREFIX"
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch -q --depth 1 origin "$BRANCH"
    git -C "$SRC" reset -q --hard FETCH_HEAD
else
    git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC"
fi

say "Python-omgeving opzetten"
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
else
    # garminconnect needs Python >= 3.12; get one with uv, kept inside $PREFIX.
    say "Systeem-Python is ouder dan 3.12, Python 3.12 installeren via uv"
    export UV_INSTALL_DIR="$PREFIX/uv" UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_NO_MODIFY_PATH=1
    [ -x "$UV_INSTALL_DIR/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | sh -s -- -q
    [ -x "$VENV/bin/python" ] || "$UV_INSTALL_DIR/uv" venv -q --seed --python 3.12 "$VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q --upgrade "$SRC"

say "Configuratie ($ENV_FILE)"
if [ ! -f "$ENV_FILE" ]; then
    install -m 600 -o root -g root "$SRC/deploy/vitadock-garmin-sync.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
set_var() { # set_var NAME VALUE: replace or append without echoing the value
    local tmp; tmp=$(mktemp)
    grep -v "^$1=" "$ENV_FILE" >"$tmp" || true
    printf '%s=%s\n' "$1" "$2" >>"$tmp"
    install -m 600 -o root -g root "$tmp" "$ENV_FILE" && rm -f "$tmp"
}
get_var() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1; }
for var in VITADOCK_CONSUMER_KEY VITADOCK_CONSUMER_SECRET; do
    if [ -z "$(get_var "$var")" ]; then
        val="${!var:-}"
        [ -n "$val" ] || val=$(ask "$var" "$var (zie README)")
        [ -n "$val" ] || die "$var is verplicht"
        set_var "$var" "$val"
    fi
done
if ! (set -a; . "$ENV_FILE"; "$VENV/bin/python" -c \
        'from vitadock_garmin_sync.vitadock import VitaDockClient; VitaDockClient()._request_token()' \
        >/dev/null 2>&1); then
    # Clear them so the next run asks again.
    set_var VITADOCK_CONSUMER_KEY ""
    set_var VITADOCK_CONSUMER_SECRET ""
    die "VitaDock weigert de consumer key/secret (zie README hoe je ze vindt). Draai de installer opnieuw."
fi

say "Hulpcommando /usr/local/bin/vitadock-garmin-sync"
cat >/usr/local/bin/vitadock-garmin-sync <<EOF
#!/usr/bin/env bash
# Runs vitadock-garmin-sync as $SVC_USER with the service configuration.
set -euo pipefail
[ "\$(id -u)" -eq 0 ] || exec sudo "\$0" "\$@"
set -a; . $ENV_FILE; set +a
export STATE_DIRECTORY=$STATE HOME=$STATE
exec setpriv --reuid=$SVC_USER --regid=$SVC_USER --init-groups -- $VENV/bin/vitadock-garmin-sync "\$@"
EOF
chmod 755 /usr/local/bin/vitadock-garmin-sync
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$STATE"

say "systemd service en timer installeren"
install -m 644 "$SRC/deploy/vitadock-garmin-sync.service" "$SRC/deploy/vitadock-garmin-sync.timer" /etc/systemd/system/
systemctl daemon-reload

if [ ! -f "$STATE/state.json" ] || [ ! -e "$STATE/garmin" ]; then
    if [ -n "$INTERACTIVE" ]; then
        say "Eenmalig inloggen bij VitaDock en Garmin (wachtwoorden worden niet opgeslagen)"
        /usr/local/bin/vitadock-garmin-sync login \
            || echo "Inloggen mislukt. Probeer opnieuw met:  sudo vitadock-garmin-sync login"
    else
        echo "Log daarna eenmalig in met:  sudo vitadock-garmin-sync login"
    fi
fi

if [ -f "$STATE/state.json" ]; then
    systemctl enable --now vitadock-garmin-sync.timer
    say "Klaar. De sync draait elke 30 minuten."
    echo "  Testen zonder upload:  sudo vitadock-garmin-sync sync --dry-run"
    echo "  Logs:                  journalctl -u vitadock-garmin-sync -f"
else
    echo "Na 'sudo vitadock-garmin-sync login':  sudo systemctl enable --now vitadock-garmin-sync.timer"
fi
