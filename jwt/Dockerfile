FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY dev_idp ./dev_idp
COPY users.json ./users.json
COPY keys ./keys
ENV PATH="/app/.venv/bin:$PATH"
USER 10001
EXPOSE 8010
CMD ["uvicorn", "dev_idp.app:app", "--host", "0.0.0.0", "--port", "8010"]
