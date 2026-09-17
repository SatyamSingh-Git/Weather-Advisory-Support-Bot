FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The policy editor writes into sops/, and nothing else on disk is ever written.
RUN useradd --system --no-create-home weatherbot && chown -R weatherbot:weatherbot /app/sops
USER weatherbot

ENV PORT=8000
EXPOSE 8000

# /health reports the policy count, so a container that boots with an unreadable rule set
# is unhealthy rather than quietly serving nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s   CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT}"]
