HOST_LAN_IP ?= $(shell ipconfig getifaddr en0 2>/dev/null || hostname -I | cut -d' ' -f1)

up:
	HOST_LAN_IP=$(HOST_LAN_IP) docker compose up -d --build
	@for i in $$(seq 30); do curl -sf -H 'Authorization: Bearer dev-token' localhost:8090/admin/public-url | grep -q '"tunnel":"' && break; sleep 1; done
	@curl -s -H 'Authorization: Bearer dev-token' localhost:8090/admin/public-url | python3 -c 'import json,sys; u=json.load(sys.stdin); [print(f"Админка ({k}): {v}/manage/") for k,v in u.items() if v]'

test:
	uv run pytest

down:
	docker compose down -v
