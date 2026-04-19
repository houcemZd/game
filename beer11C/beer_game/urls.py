from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.http import HttpResponse

def health(request):
    return HttpResponse("ok")

urlpatterns = [
    path('health/', health, name='health'),
    path('admin/', admin.site.urls),
    path('favicon.ico', RedirectView.as_view(url='/static/game/favicon.svg', permanent=True)),
    path('', include('game.urls')),
]
