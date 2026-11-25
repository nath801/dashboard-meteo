# Image durcie (audit de sécurité : V6)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Utilisateur non privilégié
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
WORKDIR /app

COPY --chown=appuser:appuser requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=appuser:appuser . .
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"

# Dans le conteneur, Streamlit écoute sur toutes les interfaces du réseau Docker interne
# (il n'est pas publié sur l'hôte : seul le proxy Caddy l'est).
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0"]
