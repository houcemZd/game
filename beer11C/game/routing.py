from django.urls import re_path
from .consumers import GameConsumer

websocket_urlpatterns = [
    # Match both "ws/game/..." and "/ws/game/..." cases reliably
    re_path(r"^/?ws/game/(?P<session_id>\d+)/(?P<token>[^/]+)/$", GameConsumer.as_asgi()),
]