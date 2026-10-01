ADMIN_TOKEN ?= dev-token
# macOS: address of the default-route interface; Linux: hostname -I
HOST_LAN_IP ?= $(shell ipconfig getifaddr $$(route -n get default 2>/dev/null | awk '/interface:/{print $$2}') 2>/dev/null || hostname -I 2>/dev/null | cut -d' ' -f1)
export ADMIN_TOKEN HOST_LAN_IP

up:
	docker compose up -d --build
	@for i in $$(seq 30); do curl -sf -o /dev/null -H "Authorization: Bearer $$ADMIN_TOKEN" localhost:8091/admin/public-url && break; sleep 1; done; \
	printf 'Открыть страницу голосования в интернет через Cloudflare-туннель, чтобы голосовать с любого телефона? Админка останется только на этом компьютере. [y/N] '; \
	read a || echo; \
	case "$$a" in y|Y|yes|д|Д|да|Да) \
		t=$$(curl -sf -X POST -H "Authorization: Bearer $$ADMIN_TOKEN" localhost:8091/admin/tunnel | grep -o 'https://[^"]*') \
			|| t="туннель не поднялся, попробуйте флажком в админке";; \
		*) t=$$(curl -sf -H "Authorization: Bearer $$ADMIN_TOKEN" localhost:8091/admin/public-url | grep -o 'https://[^"]*') \
			&& t="$$t (открыт раньше, закрыть можно флажком в админке)" || t="закрыт, открыть можно флажком в админке";; esac; \
	echo "Админка: http://localhost:8091/manage/ (токен: $$ADMIN_TOKEN)"; \
	echo "Зрители через интернет: $$t"; \
	echo "Зрители в локальной сети: $${HOST_LAN_IP:+http://$$HOST_LAN_IP:8090}"

test:
	uv run pytest

down:
	docker compose down -v

populate:
	uv run python scripts/populate.py

loadtest:
	uv run python scripts/loadtest.py
