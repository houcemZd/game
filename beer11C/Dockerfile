FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app/

# Ensure production staticfiles backend is used during collectstatic.
ENV DEBUG=False
RUN python manage.py collectstatic --no-input

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "beer_game.asgi:application"]
