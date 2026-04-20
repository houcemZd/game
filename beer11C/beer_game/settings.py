import os
import socket as _socket
from pathlib import Path
from urllib.parse import urlsplit
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

LOGIN_URL           = '/accounts/login/'
LOGIN_REDIRECT_URL  = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# ── Security ──────────────────────────────────────────────────────────────────
# Set SECRET_KEY via environment variable in production.
# A fallback is provided for local development only; never use it in production.
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-beer-game-dev-key-change-in-production',
)

DEBUG = os.environ.get('DEBUG', 'True') == 'True'

def _normalize_host(value):
    host = value.strip()
    if len(host) >= 2 and (
        (host.startswith('"') and host.endswith('"'))
        or (host.startswith("'") and host.endswith("'"))
    ):
        host = host[1:-1].strip()
    if not host:
        return ''
    if host == '*':
        return host

    if '://' in host:
        return (urlsplit(host).hostname or '').strip()

    if host.startswith('['):
        closing_bracket = host.find(']')
        if closing_bracket != -1:
            remainder = host[closing_bracket + 1:].strip()
            if remainder:
                if not remainder.startswith(':'):
                    return ''
                if not remainder[1:].isdigit():
                    return ''
            return host[1:closing_bracket].strip()

    parsed = urlsplit(f'//{host}')
    if parsed.hostname:
        return parsed.hostname.strip()

    if host.count(':') == 1:
        maybe_host, maybe_port = host.rsplit(':', 1)
        if maybe_port.isdigit():
            return maybe_host.strip()

    return host


def _build_allowed_hosts():
    base_hosts = ['localhost', '127.0.0.1']
    configured_hosts = [
        _normalize_host(h)
        for h in os.environ.get('ALLOWED_HOSTS', '').split(',')
        if h.strip()
    ]
    railway_host = _normalize_host(os.environ.get('RAILWAY_PUBLIC_DOMAIN', ''))
    if railway_host:
        configured_hosts.append(railway_host)

    seen = set()
    hosts = []
    for host in base_hosts + configured_hosts:
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


ALLOWED_HOSTS = _build_allowed_hosts()

# CSRF trusted origins — set via env for cross-device access
_extra_origins = [o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()]
CSRF_TRUSTED_ORIGINS = ['http://localhost:8000'] + _extra_origins

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',   # ← Django Channels
    'game',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise must come immediately after SecurityMiddleware
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'beer_game.urls'

# ── Channels: use ASGI, not WSGI ────────────────────────────────────────────
ASGI_APPLICATION = 'beer_game.asgi.application'

# ── Channel layer: prefer REDIS_URL env var, then local Redis, else in-memory ─
_redis_url = os.environ.get('REDIS_URL', '')

def _redis_available():
    try:
        s = _socket.create_connection(("127.0.0.1", 6379), timeout=1)
        s.close()
        return True
    except OSError:
        return False

if _redis_url:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [_redis_url]},
        }
    }
elif _redis_available():
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [("127.0.0.1", 6379)]},
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

DATABASES = {
    'default': dj_database_url.config(
        default='sqlite:///' + str(BASE_DIR / 'db.sqlite3'),
        conn_max_age=600,
    )
}

# ── Static files ──────────────────────────────────────────────────────────────
STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# WhiteNoise compressed manifest storage for production
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if not DEBUG
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Password validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Reverse-proxy trust (Railway puts TLS in front) ──────────────────────────
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ── HTTPS / cookie security (active in production when DEBUG=False) ───────────
if not DEBUG:
    SECURE_SSL_REDIRECT          = True
    SECURE_HSTS_SECONDS          = 31536000   # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD          = True
    SESSION_COOKIE_SECURE        = True
    SESSION_COOKIE_HTTPONLY      = True
    CSRF_COOKIE_SECURE           = True
    CSRF_COOKIE_HTTPONLY         = True

# ── Logging ───────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'game': {
            'handlers': ['console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': False,
        },
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
