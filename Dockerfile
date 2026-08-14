FROM python:3.13.7-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.13.7-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data \
    SEED_LIST_PATH=/app/seed/libraryMasterList.json

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

WORKDIR /app
COPY --chown=app:app seed/libraryMasterList.json /app/seed/libraryMasterList.json
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8080 8081

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "60", "--graceful-timeout", "25", "--access-logfile", "-", "--error-logfile", "-", "master_list_creation.webapp:create_app()"]
