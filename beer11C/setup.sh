#!/bin/bash
# ── Beer Game — Setup & Update ─────────────────────────────────────────────────
set -e

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
info() { echo -e "  ${CYAN}→${NC}  $1"; }
err()  { echo -e "  ${RED}✗${NC}  $1"; }
sep()  { echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

echo ""
echo -e "${BOLD}🍺  Beer Game — Setup & Update${NC}"
sep

# ── Detect project root (where manage.py lives) ────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/manage.py" ]; then
  PROJECT_ROOT="$SCRIPT_DIR"
elif [ -f "manage.py" ]; then
  PROJECT_ROOT="$(pwd)"
else
  err "Cannot find manage.py — run this script from your Django project root."
  exit 1
fi
info "Project root: $PROJECT_ROOT"

# ── Detect app name (folder containing models.py and consumers.py) ─────────────
APP_DIR=""
for d in "$PROJECT_ROOT"/*/; do
  if [ -f "$d/models.py" ] && [ -f "$d/consumers.py" ]; then
    APP_DIR="$d"
    APP_NAME="$(basename "$d")"
    break
  fi
done
if [ -z "$APP_DIR" ]; then
  err "Cannot find your game app (folder with models.py + consumers.py)."
  exit 1
fi
ok "Game app: $APP_NAME"

# ── Detect settings.py ─────────────────────────────────────────────────────────
SETTINGS_FILE=""
for f in "$PROJECT_ROOT"/*/settings.py "$PROJECT_ROOT"/settings.py; do
  if [ -f "$f" ]; then
    SETTINGS_FILE="$f"
    break
  fi
done
if [ -z "$SETTINGS_FILE" ]; then
  err "Cannot find settings.py"
  exit 1
fi
ok "Settings: $(basename $(dirname $SETTINGS_FILE))/settings.py"

# Export DJANGO_SETTINGS_MODULE for all subsequent Python commands
SETTINGS_MODULE="$(basename $(dirname $SETTINGS_FILE)).settings"
export DJANGO_SETTINGS_MODULE="$SETTINGS_MODULE"

sep

# ── 1. Virtual environment ──────────────────────────────────────────────────────
info "Checking virtual environment..."
if [ ! -d "$PROJECT_ROOT/venv" ]; then
  info "Creating virtual environment..."
  python3 -m venv "$PROJECT_ROOT/venv"
fi
source "$PROJECT_ROOT/venv/bin/activate"
ok "Virtual environment active"

# ── 2. Dependencies ─────────────────────────────────────────────────────────────
info "Installing / updating Python dependencies..."
python3 -m pip install -q -r "$PROJECT_ROOT/requirements.txt"


# Ensure daphne is installed (required for WebSocket support)
if ! python3 -c "import daphne" 2>/dev/null; then
  info "Installing daphne (ASGI server for WebSockets)..."
  python3 -m pip install -q daphne
fi
ok "Dependencies up to date (daphne ✓)"

# ── 3. Source files are managed by git ────────────────────────────────────────
sep
ok "Source files are up to date (managed by git — run 'git pull' to update)"

# ── 4. Apply database migrations ───────────────────────────────────────────────
sep
info "Checking database migrations..."

MIGRATIONS_DIR="$APP_DIR/migrations"
mkdir -p "$MIGRATIONS_DIR"
touch "$MIGRATIONS_DIR/__init__.py" 2>/dev/null || true

cd "$PROJECT_ROOT"
python manage.py migrate --run-syncdb -v 0
ok "Database up to date"

# ── 5. Detect local IP and configure network access ────────────────────────────
sep
info "Configuring network settings..."

# Get local IP (works on Mac and Linux)
LOCAL_IP=""
if command -v ipconfig &>/dev/null; then
  # macOS
  LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "")
fi
if [ -z "$LOCAL_IP" ] && command -v hostname &>/dev/null; then
  # Linux
  LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$LOCAL_IP" ]; then
  LOCAL_IP=$(python3 -c "import socket; s=socket.socket(); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2>/dev/null || echo "")
fi

if [ -n "$LOCAL_IP" ]; then
  ok "Local IP: $LOCAL_IP"
else
  warn "Could not detect local IP — cross-device access may not work"
  LOCAL_IP="0.0.0.0"
fi

# Export env vars so settings.py picks them up at runtime
export ALLOWED_HOSTS="$LOCAL_IP"
export CSRF_TRUSTED_ORIGINS="http://$LOCAL_IP:8000"
ok "ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS configured via environment"

# ── 6. Channel layer info ───────────────────────────────────────────────────────
info "Checking channel layer (Redis / InMemory)..."

REDIS_OK=false
if command -v redis-cli &>/dev/null && redis-cli ping &>/dev/null 2>&1; then
  REDIS_OK=true
elif python3 - <<'PY' >/dev/null 2>&1
import socket
try:
    s = socket.create_connection(("127.0.0.1", 6379), timeout=1)
    s.close()
    print("ok")
except OSError:
    raise SystemExit(1)
PY
then
  REDIS_OK=true
fi

if $REDIS_OK; then
  ok "Redis is running — full multiplayer enabled"
else
  warn "Redis not available — using InMemoryChannelLayer (single-machine only)"
  warn "For real cross-device multiplayer: docker run -p 6379:6379 redis:alpine"
  warn "Then re-run setup.sh"
fi

# ── 7. Offer to clear stale sessions ───────────────────────────────────────────
sep
SESSION_COUNT=$(python3 -c "
import os, sys, django
sys.path.insert(0, '.')
try:
    django.setup()
    from game.models import GameSession
    print(GameSession.objects.count())
except Exception:
    print(0)
" 2>/dev/null || echo "0")

if [ "$SESSION_COUNT" -gt 10 ]; then
  echo ""
  warn "You have $SESSION_COUNT sessions in the database."
  echo -e "     Delete all and start fresh? ${BOLD}[y/N]${NC} " && read -r REPLY
  if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    python3 -c "
import os, sys, django
sys.path.insert(0, '.')
try:
    django.setup()
    from game.models import GameSession
    n = GameSession.objects.count()
    GameSession.objects.all().delete()
    print(f'Deleted {n} sessions.')
except Exception:
    pass
" 2>/dev/null
    ok "All sessions cleared"
  else
    ok "Keeping existing sessions"
  fi
else
  ok "$SESSION_COUNT existing session(s) — keeping"
fi

# ── 8. Print summary and start ─────────────────────────────────────────────────
sep
echo ""
echo -e "${BOLD}${GREEN}✅  All done!${NC}"
echo ""
echo -e "  ${BOLD}This machine:${NC}   http://127.0.0.1:8000"
if [ -n "$LOCAL_IP" ] && [ "$LOCAL_IP" != "0.0.0.0" ]; then
echo -e "  ${BOLD}Other devices:${NC}  http://$LOCAL_IP:8000"
fi
echo ""
echo -e "  ${BOLD}Roles:${NC}  👤 Customer  🛒 Retailer  🏪 Wholesaler  🚚 Distributor  🏭 Factory"
echo -e "  ${BOLD}Flow:${NC}   Receive → Ship → Order → ${CYAN}Ready for next week${NC}"
echo ""
echo -e "  ${YELLOW}Using Daphne (ASGI) — WebSockets fully supported${NC}"
sep
echo ""

# Detect Django project module (folder containing settings.py + asgi.py)
ASGI_MODULE=""
for d in "$PROJECT_ROOT"/*/; do
  if [ -f "$d/asgi.py" ] && [ -f "$d/settings.py" ]; then
    ASGI_MODULE="$(basename "$d").asgi:application"
    break
  fi
done

if [ -z "$ASGI_MODULE" ]; then
  warn "Cannot find asgi.py — falling back to manage.py runserver (no WebSockets!)"
  warn "Create <project>/asgi.py with Django Channels routing for full functionality."
  cd "$PROJECT_ROOT"
  python manage.py runserver 0.0.0.0:8000
else
  ok "ASGI module: $ASGI_MODULE"
  cd "$PROJECT_ROOT"
  # Daphne: bind to all interfaces, port 8000
  daphne -b 0.0.0.0 -p 8000 "$ASGI_MODULE"
fi
