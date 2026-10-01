"""Нагрузочный тест на поднятом стенде (make up): приём на ядро при 1/2/4 репликах, этап 1 при 1/2/4 процессах, время до итога.

Генератор — этот же файл в режиме `gen` внутри сети compose (сервис `load`): бьёт прямо в ingest:8000,
минуя nginx и проброс портов. CPU приёма — из cgroup контейнеров только за окно замера.
"""
import json, multiprocessing, os, platform, random, socket, subprocess, sys, time, urllib.request, uuid
from datetime import datetime, timedelta, timezone

ADMIN, TOKEN = "http://localhost:8091/admin", os.environ.get("ADMIN_TOKEN", "dev-token")
REPLICAS = [int(n) for n in os.environ.get("REPLICAS", "1 2 4").split()]
WORKERS = [int(n) for n in os.environ.get("WORKERS", "1 2 4").split()]  # процессов этапа 1 (STAGE1_WORKERS)
WARMUP, SECONDS = 3, int(os.environ.get("LOAD_SECONDS", "15"))  # CPU меряется только за SECONDS после прогрева
PROCS, CONNS = 2, 32  # на реплику: процессов генератора и соединений keep-alive в каждом
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRST_OCTETS = [5, 31, 46, 77, 85, 91, 95, 176, 178, 188, 217]


# ---------- генератор (в контейнере) ----------

def random_fingerprint_json(rnd):  # тот же состав, что шлёт страница (fingerprint.md), значения — случайные
    os_name, engine, browser = rnd.choice([("iOS", "WebKit", "Safari"), ("Android", "Blink", "Chrome"),
                                           ("Android", "Blink", "YaBrowser"), ("Windows", "Blink", "Chrome")])
    return json.dumps({"os": os_name, "engine": engine, "browser": browser, "version": str(rnd.randint(120, 141)),
                       "model": rnd.choice([None, "SM-A546E", "Pixel 8", "23129RAA4G", "RMX3710", "SM-S918B"]),
                       "display": {"dpr": rnd.choice([1, 2, 2.625, 3]), "colorDepth": 24, "gamut": rnd.choice(["srgb", "p3"]),
                                   "dynamicRange": "standard"},
                       "language": rnd.choice(["ru", "en"]), "locale": "ru-RU", "calendar": "gregory", "hourCycle": "h23",
                       "timezone": rnd.choice(["Europe/Moscow", "Asia/Novosibirsk", "Europe/Samara", "Asia/Yekaterinburg"]),
                       "media": {"colorScheme": rnd.choice(["light", "dark"]), "reducedMotion": "no-preference",
                                 "contrast": "no-preference", "invertedColors": "none", "forcedColors": "none"}},
                      separators=(",", ":"))


def generator_process(host, slot, counts, until, poll_id):
    import asyncio, uvloop
    rnd = random.Random()
    fingerprints = [random_fingerprint_json(rnd).encode() for _ in range(1000)]
    poll_id = poll_id.encode()

    async def keep_alive_client():
        reader = writer = None
        while time.time() < until:
            try:
                if writer is None:
                    reader, writer = await asyncio.open_connection(host, 8000)
                body = b'{"poll_id":"%s","options":[%d],"voter_id":"%s","fp":%s}' % (
                    poll_id, rnd.randrange(4), str(uuid.uuid4()).encode(), rnd.choice(fingerprints))
                ip = b"%d.%d.%d.%d" % (rnd.choice(FIRST_OCTETS), rnd.randrange(256), rnd.randrange(256), rnd.randrange(1, 255))
                writer.write(b"POST /api/vote HTTP/1.1\r\nHost: ingest\r\nContent-Type: application/json\r\n"
                             b"X-Forwarded-For: %s\r\nContent-Length: %d\r\n\r\n%s" % (ip, len(body), body))
                head = await reader.readuntil(b"\r\n\r\n")
                length_header_at = head.lower().find(b"content-length:")
                if length_header_at >= 0:
                    length_start = length_header_at + len(b"content-length:")
                    await reader.readexactly(int(head[length_start:head.index(b"\r\n", length_header_at)]))
                if head[9:12] == b"204":
                    counts[slot] += 1
                else:
                    counts[slot + 1] += 1
            except (OSError, asyncio.IncompleteReadError):
                counts[slot + 1] += 1
                writer = None

    async def run_clients():
        await asyncio.gather(*(keep_alive_client() for _ in range(CONNS)))
    uvloop.run(run_clients())


def gen(poll_id, seconds):
    hosts = socket.gethostbyname_ex("ingest")[2]  # все реплики; каждый процесс — на свою
    process_count = PROCS * len(hosts)
    counts = multiprocessing.Array("q", 2 * process_count, lock=False)  # [204, не 204] на процесс
    until = time.time() + WARMUP + seconds + 1
    processes = [
        multiprocessing.Process(target=generator_process, args=(hosts[i % len(hosts)], 2 * i, counts, until, poll_id))
        for i in range(process_count)
    ]
    for process in processes:
        process.start()

    def answered_204():
        return sum(counts[0::2])

    print("hosts", len(hosts), flush=True)
    time.sleep(WARMUP)
    answered_at_go, go_at = answered_204(), time.time()
    print("go", flush=True)
    time.sleep(seconds)
    print("stop", (answered_204() - answered_at_go) / (time.time() - go_at), flush=True)
    for process in processes:
        process.join()
    print("total", answered_204(), sum(counts[1::2]), flush=True)


# ---------- оркестровка (на хосте) ----------

def run(*command):
    return subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def api(method, path, body=None):
    data = None
    if body:
        data = json.dumps(body).encode()
    request = urllib.request.Request(ADMIN + path, method=method, data=data,
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or "null")


def cpu_seconds(container_ids):  # usage_usec из cgroup всех контейнеров, параллельно
    readers = [subprocess.Popen(["docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"], stdout=subprocess.PIPE, text=True)
               for container in container_ids]
    return sum(int(reader.communicate()[0].split()[1]) for reader in readers) / 1e6


def rss_mib(container):
    status = run("docker", "exec", container, "cat", "/proc/1/status")
    vm_rss = next(line for line in status.splitlines() if line.startswith("VmRSS"))
    return int(vm_rss.split()[1]) / 1024


def measure_ingest(poll_id, replicas, seconds=SECONDS):
    run("docker", "compose", "up", "-d", "--no-deps", "--scale", f"ingest={replicas}", "ingest")
    time.sleep(5)  # новые реплики подключаются к Kafka и читают опросы
    container_ids = run("docker", "compose", "ps", "-q", "ingest").split()
    generator = subprocess.Popen(
        ["docker", "compose", "run", "--rm", "--no-deps", "-T", "load", "python", "scripts/loadtest.py", "gen", poll_id, str(seconds)],
        cwd=ROOT, stdout=subprocess.PIPE, text=True)
    for line in generator.stdout:
        if not line.split():  # пустые строки в выводе docker compose run
            continue
        word, *rest = line.split()
        if word == "hosts" and int(rest[0]) != replicas:
            sys.exit(f"генератор видит {rest[0]} реплик вместо {replicas}")
        if word in ("go", "stop"):
            before = time.time()
            cpu = cpu_seconds(container_ids)
            measured_at = (before + time.time()) / 2
            if word == "go":
                cpu_at_go, go_at = cpu, measured_at
            else:
                votes_per_second, cores = float(rest[0]), (cpu - cpu_at_go) / (measured_at - go_at)
        if word == "total":
            answered_204, not_204 = int(rest[0]), int(rest[1])
    if generator.wait():
        sys.exit("генератор упал")
    return votes_per_second, cores, answered_204, not_204


def machine():
    if sys.platform == "darwin":
        cpu = run("sysctl", "-n", "machdep.cpu.brand_string")
        host_os = f"macOS {platform.mac_ver()[0]}"
    else:
        cpu = platform.processor()
        host_os = platform.platform()
    docker_cpus, docker_memory, docker_version, docker_os = run(
        "docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.ServerVersion}} {{.OperatingSystem}}").split(" ", 3)
    return (f"{cpu}, {os.cpu_count()} ядер, {host_os} {platform.machine()}; "
            f"{docker_os} {docker_version}: {docker_cpus} CPU, {int(docker_memory) / 2**30:.1f} ГиБ")


def new_poll():
    now = datetime.now(timezone.utc)
    poll = api("POST", "/polls", {"question": "loadtest", "type": "single", "options": ["A", "B", "C", "D"],
                                   "window_start": now.isoformat(), "window_end": (now + timedelta(minutes=30)).isoformat(), "grace_s": 0})
    api("POST", f"/polls/{poll['id']}/activate")
    return poll["id"]


def forwarded(rpk_container):  # записей в votes_by_ip: этап 1 пересылает туда каждый новый voter_id сразу, а received обновляет раз в секунду
    out = run("docker", "exec", rpk_container, "rpk", "topic", "describe", "votes_by_ip", "-p", "-X", "brokers=redpanda:9092")
    return sum(int(line.split()[-1]) for line in out.splitlines()[1:])  # HIGH-WATERMARK


def measure_stage1(workers, poll_id, n_votes):
    """workers временных процессов этапа 1 догоняют отставание опроса (основной stage1 остановлен), потом итог и удаление опроса."""
    rpk_container = run("docker", "compose", "run", "-d", "--no-deps", "--entrypoint", "sleep", "topics", "infinity")  # rpk не в cgroup брокера
    forwarded_before = forwarded(rpk_container)
    starts = [subprocess.Popen(["docker", "compose", "run", "-d", "--no-deps",
                                "-e", f"STAGE1_WORKERS={workers}", "-e", f"STAGE1_WORKER_INDEX={index}", "stage1"],
                               cwd=ROOT, stdout=subprocess.PIPE, text=True) for index in range(workers)]  # стартуют одновременно
    container_ids = [start.communicate()[0].strip() for start in starts]
    redpanda = run("docker", "compose", "ps", "-q", "redpanda")
    try:
        # скорость и CPU — по пересылке от 10 % до 90 % отставания (все голоса теста — разные voter_id): без разброса
        # старта процессов и шага их отчётов в 1 с, которые при 4 процессах сравнимы со всем догоном (~4 с)
        at_10_percent = None
        while True:
            done = forwarded(rpk_container) - forwarded_before
            if done >= 0.9 * n_votes:
                break
            if done >= 0.1 * n_votes and not at_10_percent:
                at_10_percent = time.time(), done, cpu_seconds(container_ids), cpu_seconds([redpanda])
        started_at, done_at_start, cpu_at_start, redpanda_cpu_at_start = at_10_percent
        elapsed = time.time() - started_at
        votes_per_second = (done - done_at_start) / elapsed
        cores = (cpu_seconds(container_ids) - cpu_at_start) / elapsed
        redpanda_cores = (cpu_seconds([redpanda]) - redpanda_cpu_at_start) / elapsed
        while api("GET", f"/polls/{poll_id}/results")["received"] < n_votes:  # итог — после полного догона
            time.sleep(0.2)
        rss = rss_mib(container_ids[0])
        api("POST", f"/polls/{poll_id}/finish")
        closed_at = time.time()
        while True:
            results = api("GET", f"/polls/{poll_id}/results")
            if results["status"] == "final":
                break
            time.sleep(0.2)
        close_to_final_s = time.time() - closed_at
        api("DELETE", f"/polls/{poll_id}")
    finally:
        run("docker", "rm", "-f", rpk_container, *container_ids)
    return votes_per_second, cores, redpanda_cores, rss, close_to_final_s, results


def main():
    run("docker", "compose", "--profile", "load", "build", "load")  # заранее: вывод сборки не смешивается с выводом генератора
    run("docker", "compose", "restart", "stage1")
    time.sleep(3)
    rss_fresh = rss_mib(run("docker", "compose", "ps", "-q", "stage1"))  # свежий процесс без опросов — точка отсчёта памяти
    run("docker", "compose", "stop", "stage1")  # этап 1 копит отставание, потом догоняет: так видна его скорость, а не скорость приёма
    ingest_rows, stage1_rows = [], []
    try:
        poll_id, n_votes = new_poll(), 0
        for replicas in REPLICAS:
            votes_per_second, cores, answered_204, not_204 = measure_ingest(poll_id, replicas)
            ingest_rows.append((replicas, votes_per_second, cores, not_204))
            n_votes += answered_204
        fill_seconds = max(1, round(n_votes / ingest_rows[-1][1]) - WARMUP - 1)  # секунд приёма до того же объёма на последнем числе реплик
        for run_number, workers in enumerate(WORKERS):
            if run_number > 0:  # своё отставание на каждое число процессов: новый опрос, этап 1 остановлен
                poll_id = new_poll()
                n_votes = measure_ingest(poll_id, REPLICAS[-1], fill_seconds)[2]
            stage1_rows.append((workers, n_votes) + measure_stage1(workers, poll_id, n_votes))
    finally:
        run("docker", "compose", "up", "-d", "--no-deps", "--scale", "ingest=2", "ingest")
        run("docker", "compose", "start", "stage1")

    print(f"\nМашина: {machine()}")
    print(f"Приём: генератор в сети compose -> ingest:8000, {PROCS * CONNS} соединений keep-alive на реплику, "
          f"CPU из cgroup за {SECONDS} с после {WARMUP} с прогрева")
    print(f"{'реплик':>7} {'голосов/с':>10} {'ядер':>6} {'на ядро':>8} {'не 204':>7}")
    for replicas, votes_per_second, cores, not_204 in ingest_rows:
        print(f"{replicas:>7} {votes_per_second:>10.0f} {cores:>6.2f} {votes_per_second / cores:>8.0f} {not_204:>7}")
    if any(not_204 for *_, not_204 in ingest_rows):
        print("ВНИМАНИЕ: не все ответы 204")
    print("Этап 1: догоняет отставание опроса (с пересылкой в votes_by_ip); скорость (по votes_by_ip) и CPU (ядер, cgroup) — от 10 % до 90 % отставания; "
          "Redpanda — один брокер стенда, --smp 1")
    print(f"{'процессов':>9} {'голосов':>8} {'голосов/с':>10} {'на процесс':>11} {'CPU этапа 1':>12} {'CPU Redpanda':>13}")
    for workers, n_votes, votes_per_second, cores, redpanda_cores, *_ in stage1_rows:
        print(f"{workers:>9} {n_votes:>8} {votes_per_second:>10.0f} {votes_per_second / workers:>11.0f} {cores:>12.2f} {redpanda_cores:>13.2f}")
    workers, n_votes, _, _, _, rss_loaded, close_to_final_s, results = stage1_rows[0]
    print(f"Память этапа 1 ({workers} процесс): RSS {rss_fresh:.0f} -> {rss_loaded:.0f} МиБ, "
          f"{(rss_loaded - rss_fresh) * 2**20 * workers / n_votes:.0f} Б на уникальный voter_id (вместе с буферами Kafka)")
    print(f"Этап 2: от закрытия окна до final {close_to_final_s:.1f} с по {n_votes} голосам (включая delivery.timeout 3 с); "
          f"received {results['received']}, total {results['total']}, counted {results['counted']}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["gen"]:
        gen(sys.argv[2], int(sys.argv[3]))
    else:
        main()
