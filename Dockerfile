FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 amadeus \
    && useradd --system --uid 10001 --gid amadeus --home-dir /app --shell /usr/sbin/nologin amadeus \
    && mkdir -p /app/data \
    && chown -R amadeus:amadeus /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=amadeus:amadeus . .

USER 10001:10001

CMD ["python", "main.py"]
