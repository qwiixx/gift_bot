FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Runtime uses only the Python standard library.
COPY bot.py ./
COPY gift_offer/ ./gift_offer/
RUN mkdir -p /app/data

EXPOSE 3000
STOPSIGNAL SIGTERM
CMD ["python", "bot.py"]
