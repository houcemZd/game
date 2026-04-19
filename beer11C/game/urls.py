from django.urls import path
from . import views
from . import accounts_views

urlpatterns = [
    # ── Accounts ──────────────────────────────────────────────────────────────
    path('accounts/login/',    accounts_views.login_view,    name='login'),
    path('accounts/register/', accounts_views.register_view, name='register'),
    path('accounts/logout/',   accounts_views.logout_view,   name='logout'),

    # ── Game ──────────────────────────────────────────────────────────────────
    path('',                                        views.home,             name='home'),
    path('new/',                                    views.new_game,         name='new_game'),
    # Multiplayer
    path('game/<int:session_id>/lobby/',            views.lobby,            name='lobby'),
    path('game/<int:session_id>/lobby-status/',     views.lobby_status,     name='lobby_status'),
    path('game/<int:session_id>/lobby-start/',      views.lobby_start_game, name='lobby_start_game'),
    path('game/<int:session_id>/lobby-chat/',       views.lobby_chat,       name='lobby_chat'),
    path('join/<str:token>/',                       views.join_game,        name='join_game'),
    path('game/<int:session_id>/play/',             views.play,             name='play'),
    path('game/<int:session_id>/customer/',         views.customer_view,    name='customer_view'),
    path('game/<int:session_id>/customer/play/',    views.customer_play,    name='customer_play'),

    path('game/<int:session_id>/',                  views.dashboard,        name='dashboard'),

    path('game/<int:session_id>/init/',             views.game_init,        name='game_init'),
    path('game/<int:session_id>/turn/',             views.next_turn,        name='next_turn'),
    path('game/<int:session_id>/reset/',            views.reset_game,       name='reset_game'),
    path('game/<int:session_id>/results/',          views.results,          name='results'),
    path('game/<int:session_id>/api/chart/',        views.chart_data_api,   name='chart_data_api'),
    path('game/<int:session_id>/view/<str:role>/',  views.client_view,      name='client_view'),

    # ── Instructor tools ──────────────────────────────────────────────────────
    path('game/<int:session_id>/instructor/',       views.instructor_view,  name='instructor_view'),
    path('game/<int:session_id>/export/csv/',       views.export_csv,       name='export_csv'),
    path('game/<int:session_id>/ai-replace/<str:role>/',
                                                    views.ai_replace_role,  name='ai_replace_role'),
]

