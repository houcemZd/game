# 🍺 Beer Game — Django Supply Chain Simulation

A full-stack implementation of the **MIT Beer Game** supply chain simulation,
built with Django + Django Channels (WebSockets) for real-time multiplayer.

## Live links

| | URL |
|---|---|
| **GitHub Pages URL (redirects to full game)** | `https://houcemzd.github.io/beergameenib.github.io/` |
| **Full multiplayer app** | `https://beergame-aaqe.onrender.com` |
| **Browser demo** | `https://houcemzd.github.io/beergameenib.github.io/demo.html` |

> **GitHub Pages** now redirects the root URL directly to the live full game.
> The multiplayer game (Django + WebSockets + Redis) is hosted on a
> backend-capable platform. A one-click **Render** blueprint is included.

---

## One-click deploy to Render

`render.yaml` at the repository root is a Render Blueprint that provisions:

- A **web service** (Python / Daphne ASGI) running the Django app
- A **PostgreSQL** database
- A **Redis** service for Django Channels

**Steps:**

1. Sign in to [render.com](https://render.com) and click **New → Blueprint**
2. Connect this repository — Render detects `render.yaml` automatically
3. Click **Apply** — Render builds, runs `collectstatic` + `migrate`, and starts Daphne
4. Copy the generated `*.onrender.com` hostname and set it as:
   - `ALLOWED_HOSTS` env var in the Render dashboard
   - `CSRF_TRUSTED_ORIGINS` env var (prefix with `https://`)
5. Update the **Play the Full Game** button URL in `index.html` and push to `main`

Alternative hosts that support ASGI + WebSockets + Redis:
**Railway.app**, **Fly.io** — use the `Procfile` inside `beer11C/`.

---

## Features

| Feature | Details |
|---|---|
| **Multiplayer mode** | 4 players, each with an isolated role view via WebSockets |
| **2-week pipeline delays** | Both orders AND shipments are delayed — the core mechanic |
| **AI fallback** | Pipeline-aware base-stock policy fills in for any missing player |
| **Real-time updates** | Week advances automatically when all players submit |
| **Charts** | Inventory, orders, backlog, cost — powered by Chart.js |
| **Bullwhip Effect Index** | σ(orders) / σ(demand) ratio per player on results page |
| **Instructor view** | Live overview + CSV export for the session creator |
| **Information hiding** | Each player sees ONLY their own inventory (multiplayer) |
| **Browser demo** | Standalone single-page game at `demo.html` — no backend needed |

---

## Project Structure

```
beer11C/                     ← Django project root (run commands from here)
├── manage.py
├── requirements.txt
├── setup.sh                 ← Optional environment setup script
│
├── beer_game/               ← Django project config
│   ├── settings.py          ← Security settings; reads SECRET_KEY/DEBUG from env
│   ├── urls.py
│   ├── asgi.py              ← ASGI entry point (required for WebSockets)
│   └── wsgi.py
│
└── game/                    ← Main application
    ├── models.py            ← GameSession, Player, PlayerSession, Pipeline models
    ├── services.py          ← Game engine (phase-gated): open/close week, AI policy
    ├── consumers.py         ← WebSocket consumer (real-time multiplayer)
    ├── views.py             ← HTTP views with session-ownership authorization
    ├── accounts_views.py    ← Login / register / logout views
    ├── routing.py           ← WebSocket URL routing
    ├── urls.py              ← HTTP URL routing
    ├── templatetags/
    │   └── game_extras.py   ← Template filters: get_item, currency, role_display…
    ├── migrations/          ← Database migrations (including indexes)
    └── templates/
        ├── accounts/
        │   ├── login.html
        │   └── register.html
        └── game/
            ├── base.html        ← Dark design system (Space Mono + DM Sans)
            ├── home.html        ← Session list
            ├── new_game.html    ← Create game (single/multi toggle)
            ├── game_init.html   ← Configure initial state
            ├── lobby.html       ← Host view: share invite links
            ├── join.html        ← Player joins with their name
            ├── play.html        ← Real-time multiplayer game screen
            ├── customer_play.html ← Real-time customer screen
            ├── dashboard.html   ← Single-player game screen
            ├── client_view.html ← Read-only per-role view
            ├── customer_view.html ← Customer demand overview
            └── results.html     ← End-game KPIs + bullwhip analysis
```

---

## Installation & Setup

### 1. Clone the repository

```bash
git clone https://github.com/houcemZd/beergame10C.git
cd beergame10C/beer11C
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file (or export variables in your shell) for production use:

```bash
export SECRET_KEY="your-strong-random-key-here"
export DEBUG="False"
export ALLOWED_HOSTS="yourdomain.com,www.yourdomain.com"
```

> **Local development:** if `SECRET_KEY` is not set, a hard-coded insecure key is
> used as a fallback. Never deploy without setting `SECRET_KEY`.

### 5. Start Redis

Redis is required for Django Channels' channel layer (real-time messaging).

**Option A — Docker (recommended):**
```bash
docker run -p 6379:6379 redis:alpine
```

**Option B — Local install:**
```bash
# Ubuntu/Debian
sudo apt install redis-server && redis-server

# Mac (Homebrew)
brew install redis && brew services start redis
```

**Option C — No Redis (local dev only):**
The server auto-detects Redis on startup. If Redis is unavailable it falls back
to `InMemoryChannelLayer` automatically. ⚠️ Only works with a single process —
do NOT use in production.

### 6. Run migrations

```bash
python manage.py migrate
```

### 7. Collect static files (production only)

```bash
python manage.py collectstatic
```

### 8. Start the server

```bash
# Development (Daphne handles both HTTP + WebSocket)
pip install daphne
daphne beer_game.asgi:application

# OR use Django's built-in runserver (works with Channels in dev mode)
python manage.py runserver
```

Open: **http://127.0.0.1:8000**

---

## How to Play (Multiplayer)

1. Click **New Game** → choose **Multiplayer** mode
2. The **Lobby** page shows 4 unique invite links — one per role
3. Share each link with a player (or open all 4 in separate browser tabs for testing)
4. Each player enters their name on the **Join** page
5. Every player sees only their own inventory and pipeline
6. Each week: enter your order quantity → click **Submit Order**
7. The week advances **automatically** once all 4 players submit
8. After all weeks complete, the **Results** page shows:
   - Total cost per player
   - Bullwhip Effect Index per role
   - Full history charts

---

## Game Mechanics

### Supply Chain Structure
```
Customer → Retailer ⇄ Wholesaler ⇄ Distributor ⇄ Factory
```

### Delays
- **Order delay:** 2 weeks (your order takes 2 weeks to reach your upstream supplier)
- **Shipment delay:** 2 weeks (goods take 2 weeks to arrive after being shipped)

### Costs (per unit, per week)
- **Holding cost:** $0.50 (for each unit sitting in inventory)
- **Backlog cost:** $1.00 (for each unit of unfilled demand)

### Demand Pattern (classic MIT pattern)
- Weeks 1–4: 4 units/week (steady state)
- Week 5+: ~8 units/week (demand shock)
- Small ±1 random noise added for realism

### AI Policy
Players who don't submit an order get the AI base-stock policy:
```
order = max(0, target - inventory - in_transit + backlog)
target = 16 units
```
This is pipeline-aware (counts goods already in transit) to avoid
artificial bullwhip amplification from the AI itself.

---

## WebSocket Message Protocol

### Client → Server
```json
{ "type": "submit_order", "quantity": 8 }
{ "type": "set_name",     "name": "Alice" }
```

### Server → Client
```json
{ "type": "state_update",   "role": "retailer", "week": 5, "own": {...}, "pipeline": [...], "history": [...] }
{ "type": "ready_status",   "submitted": ["retailer", "wholesaler"], "connected": [...], "total": 4 }
{ "type": "player_joined",  "role": "wholesaler", "name": "Bob" }
{ "type": "player_left",    "role": "wholesaler", "name": "Bob" }
{ "type": "game_over",      "results_url": "/game/1/results/" }
{ "type": "error",          "message": "Order must be between 0 and 200" }
```

---

## Bullwhip Effect

The Bullwhip Effect Index measures how much each role **amplifies** demand variability:

```
BWE = σ(orders placed by role) / σ(customer demand)
```

| Score | Interpretation |
|---|---|
| ~1.0 | Perfect — no amplification |
| 1.5–3.0 | Typical — mild bullwhip |
| 3.0–6.0 | Severe bullwhip effect |
| >6.0 | Extreme panic ordering |

In a well-run game, the Factory typically has the highest BWE score —
demonstrating how information asymmetry causes amplification up the chain.

---

## GitHub Pages

The workflow `.github/workflows/deploy-pages.yml` deploys the repository root to
GitHub Pages automatically on every push to `main`.

The Pages site serves:
- `index.html` — immediate redirect to the full hosted app
- `demo.html` — standalone browser demo (no backend required)

To enable Pages in a fresh fork:
1. Push to `main`
2. In repository settings → **Pages → Source** = **GitHub Actions**
3. `https://<username>.github.io/<repo>/` redirects to the full game

---

## Deployment (Production)

### Option A — Render (recommended, blueprint included)

See the **One-click deploy to Render** section at the top of this README.
The `render.yaml` blueprint handles everything automatically.

### Option B — Manual (any ASGI host)

```bash
export SECRET_KEY="your-real-secret-key"
export DEBUG="False"
export ALLOWED_HOSTS="your-domain.com"
export DATABASE_URL="postgres://..."   # optional; defaults to SQLite
export REDIS_URL="redis://..."         # optional; falls back to in-memory
pip install daphne
cd beer11C
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --no-input
daphne -b 0.0.0.0 -p 8000 beer_game.asgi:application
```

Supported hosting platforms:
- **Render.com** — blueprint included in `render.yaml`
- **Railway.app** — supports Redis + WebSockets natively; use `Procfile`
- **Fly.io** — Docker-based, full WebSocket support; use `Procfile`

---

## Running Tests

```bash
cd beer11C
python manage.py test game.tests --verbosity=2
```

The test suite covers models, the game engine (services), HTTP views (including
authorization checks), and template filters — 146 tests in total.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend framework | Django 4.2+ |
| Real-time / WebSockets | Django Channels 4.0+ |
| Channel layer / pub-sub | Redis via channels-redis |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Frontend charts | Chart.js 4.4 |
| Fonts | Space Mono + DM Sans (Google Fonts) |
| Deployment server | Daphne (ASGI) |

---

## Academic Context

This project implements the supply chain simulation originally developed at MIT's
Sloan School of Management. The Beer Game is used to demonstrate:

- The **Bullwhip Effect** — demand variability amplification upstream
- The cost of **information asymmetry** in supply chains
- The benefit of **pipeline-aware ordering policies**
- How **delays** create instability even with rational actors
