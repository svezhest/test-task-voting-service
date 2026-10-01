"""Everything a person may want to look up or turn. Each value can be overridden by an environment variable of the same name."""
import os


def setting(name, default, cast=str):
    return cast(os.environ.get(name, default))


# Connections
DATABASE_URL = setting("DATABASE_URL", "postgresql://postgres:postgres@postgres/postgres")
KAFKA_BOOTSTRAP = setting("KAFKA_BOOTSTRAP", "redpanda:9092")
ADMIN_TOKEN = setting("ADMIN_TOKEN", "dev-token")
WEB_ROOT = setting("WEB_ROOT", "/srv/web")
HOST_LAN_IP = setting("HOST_LAN_IP", "")
HOST_TZ = setting("HOST_TZ", "")  # the admin shows times on the clock of the computer running the stand
TRUST_XFF = setting("TRUST_XFF", "0") == "1"

# Workers: a process reads partitions p with p % WORKERS == WORKER_INDEX
PARTITION_COUNT = 8
STAGE1_WORKERS = setting("STAGE1_WORKERS", 1, int)
STAGE1_WORKER_INDEX = setting("STAGE1_WORKER_INDEX", 0, int)
STAGE2_WORKERS = setting("STAGE2_WORKERS", 1, int)
STAGE2_WORKER_INDEX = setting("STAGE2_WORKER_INDEX", 0, int)

# Deduplication knobs (docs/architecture_deduplication.md)
HONEST_QUANTILE = setting("HONEST_QUANTILE", 0.9999, float)
SECONDS_PER_REPEAT = setting("SECONDS_PER_REPEAT", 5, float)
IP_CEILING_PER_MINUTE = setting("IP_CEILING_PER_MINUTE", 5000, int)

# Timeouts and intervals, seconds
DELIVERY_TIMEOUT_S = setting("DELIVERY_TIMEOUT_S", 3, float)
POSTGRES_CONNECT_TIMEOUT_S = setting("POSTGRES_CONNECT_TIMEOUT_S", 2, float)
POLLS_QUERY_TIMEOUT_S = setting("POLLS_QUERY_TIMEOUT_S", 2, float)
POLLS_REFRESH_INTERVAL_S = setting("POLLS_REFRESH_INTERVAL_S", 1, float)
MISS_RELOAD_INTERVAL_S = setting("MISS_RELOAD_INTERVAL_S", 0.2, float)
UNKNOWN_POLL_MEMORY_S = setting("UNKNOWN_POLL_MEMORY_S", 1, float)
CLOSED_POLL_MEMORY_S = setting("CLOSED_POLL_MEMORY_S", 86400, float)
KAFKA_POLL_TIMEOUT_S = setting("KAFKA_POLL_TIMEOUT_S", 1, float)
STAGE1_REPORT_INTERVAL_S = setting("STAGE1_REPORT_INTERVAL_S", 1, float)
STAGE2_LOOP_INTERVAL_S = setting("STAGE2_LOOP_INTERVAL_S", 1, float)
STAGE2_FETCH_MAX_WAIT_S = setting("STAGE2_FETCH_MAX_WAIT_S", 0.02, float)
TUNNEL_START_TIMEOUT_S = setting("TUNNEL_START_TIMEOUT_S", 30, float)
CLOUDFLARED_METRICS_TIMEOUT_S = setting("CLOUDFLARED_METRICS_TIMEOUT_S", 1, float)
