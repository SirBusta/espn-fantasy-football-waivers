FROM python:3.12-slim

ARG VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    APP_VERSION=${VERSION}

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ .

# Starts as root only to hand /data to PUID:PGID (99:100 by default), then
# drops to that user before doing anything else.
VOLUME ["/data"]
CMD ["python", "notifier.py"]
