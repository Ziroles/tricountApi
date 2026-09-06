FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPLITTICKET_HOST=0.0.0.0 \
    SPLITTICKET_PORT=8787 \
    SPLITTICKET_DATA_DIR=/data

# Utilisateur non-root : le service n'a besoin d'écrire que dans /data.
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Base SQLite, photos des tickets et identifiants d'appareil Tricount.
RUN mkdir -p /data && chown -R appuser:appuser /data /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

USER appuser

EXPOSE 8787

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/health').read()" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
