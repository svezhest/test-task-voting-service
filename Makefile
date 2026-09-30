ADMIN_TOKEN ?= dev-token
# macOS: address of the default-route interface; Linux: hostname -I
HOST_LAN_IP ?= $(shell ipconfig getifaddr $$(route -n get default 2>/dev/null | awk '/interface:/{print $$2}') 2>/dev/null || hostname -I 2>/dev/null | cut -d' ' -f1)
export ADMIN_TOKEN HOST_LAN_IP

up:
	docker compose up -d --build
	@for i in $$(seq 30); do t=$$(curl -sf -H "Authorization: Bearer $$ADMIN_TOKEN" localhost:8091/admin/public-url | grep -o 'https://[^"]*') && break; sleep 1; done; \
	echo "Админка: http://localhost:8091/manage/ (токен: $$ADMIN_TOKEN)"; \
	echo "Зрители через туннель: $${t:-туннель не поднялся — используйте ссылку в локальной сети}"; \
	echo "Зрители в локальной сети: $${HOST_LAN_IP:+http://$$HOST_LAN_IP:8090}"

test:
	uv run pytest

down:
	docker compose down -v
