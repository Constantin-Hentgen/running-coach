FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Copie des fichiers de configuration des dépendances
COPY pyproject.toml .

# Installation des dépendances avec uv (sans environnement virtuel dans le conteneur)
RUN uv pip install --system -r pyproject.toml

# Copie du code source
COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]