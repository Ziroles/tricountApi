FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRICOUNT_RELAY_HOST=0.0.0.0 \
    TRICOUNT_RELAY_PORT=8787

# Création d'un utilisateur non-root pour la sécurité
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Création du dossier pour persister les identifiants
RUN mkdir -p /data && chown -R appuser:appuser /data /app

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code source
COPY relay.py .

# Utilisation de l'utilisateur non-root
USER appuser

# Exposition du port du relais
EXPOSE 8787

# Configuration par défaut du chemin vers les credentials
ENV TRICOUNT_CREDENTIALS_PATH=/data/.tricount-credentials.json

# Volume pour conserver les identifiants générés
VOLUME ["/data"]

CMD ["python", "relay.py"]
