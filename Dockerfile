FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.9 /uv /usr/local/bin/uv
# admin starts it on POST /admin/tunnel (Cloudflare quick tunnel to the viewer entrance)
COPY --from=cloudflare/cloudflared:2026.9.3 /usr/local/bin/cloudflared /usr/local/bin/cloudflared
WORKDIR /code
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app app
ENV PATH=/code/.venv/bin:$PATH
