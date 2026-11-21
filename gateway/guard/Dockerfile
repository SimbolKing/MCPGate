FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY guard/pyproject.toml guard/uv.lock ./
RUN uv sync --frozen --no-dev
COPY guard/app ./app
COPY guard/console ./console
COPY generated/tools.manifest.json ./tools.manifest.json
RUN mkdir /data && chown 10001:10001 /data
ENV PATH="/app/.venv/bin:$PATH"
USER 10001
EXPOSE 8020
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8020", "--workers", "1"]
