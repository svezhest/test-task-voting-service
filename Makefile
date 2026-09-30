up:
	docker compose up -d --build

test:
	uv run pytest

down:
	docker compose down -v
