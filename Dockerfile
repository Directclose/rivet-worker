FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY rivet ./rivet
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    RIVET_DATA_DIR=/var/lib/rivet
EXPOSE 8787
CMD ["python", "-m", "rivet.service"]

