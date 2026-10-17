FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 MALLOC_ARENA_MAX=2
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY camera_review /app/camera_review
COPY static /app/static
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
COPY third_party /app/third_party
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "--bind", "0.0.0.0:8090", "--workers", "1", "--threads", "4", "--timeout", "90", "camera_review.web:app"]
