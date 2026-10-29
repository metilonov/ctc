FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-tested.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-tested.txt && python -c "import libtorrent; print(libtorrent.version)"
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
COPY --chown=bot:bot . .
USER 10001:10001
VOLUME ["/data"]
CMD ["python", "main.py"]
