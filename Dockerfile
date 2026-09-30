FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.9 /uv /usr/local/bin/uv
WORKDIR /code
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app app
ENV PATH=/code/.venv/bin:$PATH
