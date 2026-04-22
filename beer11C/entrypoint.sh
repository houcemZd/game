#!/bin/sh
set -e

python manage.py migrate --no-input

exec daphne -b 0.0.0.0 -p "${PORT:-8000}" beer_game.asgi:application
