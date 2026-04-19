"""
ASGI config — replaces wsgi.py when using Django Channels.
Handles both HTTP (Django views) and WebSocket (Channels consumers).
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "beer_game.settings")

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

# Initialize Django ASGI application early to ensure the AppRegistry is populated
django_asgi_app = get_asgi_application()

# Import routing only after Django is set up
import game.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(game.routing.websocket_urlpatterns)
    ),
})