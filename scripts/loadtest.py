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


# ---------- генератор (в контейнере) ----------

def fingerprint(rnd):  # тот же состав, что шлёт страница (fingerprint.md), значения — случайные
    os_, engine, browser = rnd.choice([("iOS", "WebKit", "Safari"), ("Android", "Blink", "Chrome"),
                                       ("Android", "Blink", "YaBrowser"), ("Windows", "Blink", "Chrome")])
    return json.dumps({"os": os_, "engine": engine, "browser": browser, "version": str(rnd.randint(120, 141)),
                       "model": rnd.choice([None, "SM-A546E", "Pixel 8", "23129RAA4G", "RMX3710", "SM-S918B"]),
                       "display": {"dpr": rnd.choice([1, 2, 2.625, 3]), "colorDepth": 24, "gamut": rnd.choice(["srgb", "p3"]),
                                   "dynamicRange": "standard"},
                       "language": rnd.choice(["ru", "en"]), "locale": "ru-RU", "calendar": "gregory", "hourCycle": "h23",
                       "timezone": rnd.choice(["Europe/Moscow", "Asia/Novosibirsk", "Europe/Samara", "Asia/Yekaterinburg"]),
                       "media": {"colorScheme": rnd.choice(["light", "dark"]), "reducedMotion": "no-preference",
                                 "contrast": "no-preference", "invertedColors": "none", "forcedColors": "none"}},
                      separators=(",", ":"))


def worker(host, slot, counts, until, poll):
    import asyncio, uvloop
    rnd = random.Random()
    fps = [fingerprint(rnd).encode() for _ in range(1000)]
    poll = poll.encode()

    async def client():
        r = w = None
        while time.time() < until:
            try:
                if w is None:
                    r, w = await asyncio.open_connection(host, 8000)
                body = b'{"poll_id":"%s","options":[%d],"voter_id":"%s","fp":%s}' % (
                    poll, rnd.randrange(4), str(uuid.uuid4()).encode(), rnd.choice(fps))
                ip = b"%d.%d.%d.%d" % (rnd.choice([5, 31, 46, 77, 85, 91, 95, 176, 178, 188, 217]),
                                       rnd.randrange(256), rnd.randrange(256), rnd.randrange(1, 255))
                w.write(b"POST /api/vote HTTP/1.1\r\nHost: ingest\r\nContent-Type: application/json\r\n"
                        b"X-Forwarded-For: %s\r\nContent-Length: %d\r\n\r\n%s" % (ip, len(body), body))
                head = await r.readuntil(b"\r\n\r\n")
                if (i := head.lower().find(b"content-length:")) >= 0:
                    await r.readexactly(int(head[i + 15:head.index(b"\r\n", i)]))
                counts[slot + (head[9:12] != b"204")] += 1
            except (OSError, asyncio.IncompleteReadError):
                counts[slot + 1] += 1
                w = None

    async def main():
        await asyncio.gather(*(client() for _ in range(CONNS)))
    uvloop.run(main())


def gen(poll, seconds):
    hosts = socket.gethostbyname_ex("ingest")[2]  # все реплики; каждый процесс — на свою
    procs = PROCS * len(hosts)
    counts = multiprocessing.Array("q", 2 * procs, lock=False)  # [ok, не 204] на процесс
    until = time.time() + WARMUP + seconds + 1
    ps = [multiprocessing.Process(target=worker, args=(hosts[i % len(hosts)], 2 * i, counts, until, poll)) for i in range(procs)]
    for p in ps:
        p.start()
    ok = lambda: sum(counts[0::2])
    print("hosts", len(hosts), flush=True)
    time.sleep(WARMUP)
    ok0, t0 = ok(), time.time()
    print("go", flush=True)
    time.sleep(seconds)
    print("stop", (ok() - ok0) / (time.time() - t0), flush=True)
    for p in ps:
        p.join()
    print("total", ok(), sum(counts[1::2]), flush=True)


# ---------- оркестровка (на хосте) ----------

def sh(*cmd):
    return subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def api(method, path, body=None):
    req = urllib.request.Request(ADMIN + path, method=method, data=body and json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "null")


def cpu_seconds(ids):  # usage_usec из cgroup всех контейнеров, параллельно
    ps = [subprocess.Popen(["docker", "exec", c, "cat", "/sys/fs/cgroup/cpu.stat"], stdout=subprocess.PIPE, text=True) for c in ids]
    return sum(int(p.communicate()[0].split()[1]) for p in ps) / 1e6


def rss_mib(container):
    status = sh("docker", "exec", container, "cat", "/proc/1/status")
    return int(next(l for l in status.splitlines() if l.startswith("VmRSS")).split()[1]) / 1024


def ingest_run(poll, n, seconds=SECONDS):
    sh("docker", "compose", "up", "-d", "--no-deps", "--scale", f"ingest={n}", "ingest")
    time.sleep(5)  # новые реплики подключаются к Kafka и читают опросы
    ids = sh("docker", "compose", "ps", "-q", "ingest").split()
    p = subprocess.Popen(["docker", "compose", "run", "--rm", "--no-deps", "-T", "load", "python", "scripts/loadtest.py", "gen", poll, str(seconds)],
                         cwd=ROOT, stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if not line.split():  # пустые строки в выводе docker compose run
            continue
        word, *rest = line.split()
        if word == "hosts" and int(rest[0]) != n:
            sys.exit(f"генератор видит {rest[0]} реплик вместо {n}")
        if word in ("go", "stop"):
            t = time.time()
            c = cpu_seconds(ids)
            t = (t + time.time()) / 2
            if word == "go":
                c0, t0 = c, t
            else:
                rps, cores = float(rest[0]), (c - c0) / (t - t0)
        if word == "total":
            total, bad = int(rest[0]), int(rest[1])
    if p.wait():
        sys.exit("генератор упал")
    return rps, cores, total, bad


def machine():
    cpu = sh("sysctl", "-n", "machdep.cpu.brand_string") if sys.platform == "darwin" else platform.processor()
    host_os = f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin" else platform.platform()
    ncpu, mem, version, name = sh("docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.ServerVersion}} {{.OperatingSystem}}").split(" ", 3)
    return f"{cpu}, {os.cpu_count()} ядер, {host_os} {platform.machine()}; {name} {version}: {ncpu} CPU, {int(mem) / 2**30:.1f} ГиБ"


def new_poll():
    now = datetime.now(timezone.utc)
    poll = api("POST", "/polls", {"question": "loadtest", "type": "single", "options": ["A", "B", "C", "D"],
                                   "window_start": now.isoformat(), "window_end": (now + timedelta(minutes=30)).isoformat(), "grace_s": 0})
    api("POST", f"/polls/{poll['id']}/activate")
    return poll["id"]


def forwarded(rpk):  # записей в votes_by_ip: этап 1 пересылает туда каждый новый voter_id сразу, а received обновляет раз в секунду
    out = sh("docker", "exec", rpk, "rpk", "topic", "describe", "votes_by_ip", "-p", "-X", "brokers=redpanda:9092")
    return sum(int(line.split()[-1]) for line in out.splitlines()[1:])  # HIGH-WATERMARK


def stage1_run(w, poll, n_votes):
    """w временных процессов этапа 1 догоняют отставание опроса (основной stage1 остановлен), потом итог и удаление опроса."""
    rpk = sh("docker", "compose", "run", "-d", "--no-deps", "--entrypoint", "sleep", "topics", "infinity")  # rpk не в cgroup брокера
    base = forwarded(rpk)
    runs = [subprocess.Popen(["docker", "compose", "run", "-d", "--no-deps", "-e", f"STAGE1_WORKERS={w}", "-e", f"STAGE1_WORKER_INDEX={i}", "stage1"],
                             cwd=ROOT, stdout=subprocess.PIPE, text=True) for i in range(w)]  # стартуют одновременно
    ids, redpanda = [p.communicate()[0].strip() for p in runs], sh("docker", "compose", "ps", "-q", "redpanda")
    try:
        # скорость и CPU — по пересылке от 10 % до 90 % отставания (все голоса теста — разные voter_id): без разброса
        # старта процессов и шага их отчётов в 1 с, которые при 4 процессах сравнимы со всем догоном (~4 с)
        first = None
        while (done := forwarded(rpk) - base) < 0.9 * n_votes:
            if done >= 0.1 * n_votes and not first:
                first = time.time(), done, cpu_seconds(ids), cpu_seconds([redpanda])
        (t0, v0, c0, r0), dt = first, time.time() - first[0]
        rate, cores, rp_cores = (done - v0) / dt, (cpu_seconds(ids) - c0) / dt, (cpu_seconds([redpanda]) - r0) / dt
        while api("GET", f"/polls/{poll}/results")["received"] < n_votes:  # итог — после полного догона
            time.sleep(0.2)
        rss = rss_mib(ids[0])
        api("POST", f"/polls/{poll}/finish")
        t_close = time.time()
        while (res := api("GET", f"/polls/{poll}/results"))["status"] != "final":
            time.sleep(0.2)
        t_final = time.time() - t_close
        api("DELETE", f"/polls/{poll}")
    finally:
        sh("docker", "rm", "-f", rpk, *ids)
    return rate, cores, rp_cores, rss, t_final, res


def main():
    sh("docker", "compose", "--profile", "load", "build", "load")  # заранее: вывод сборки не смешивается с выводом генератора
    sh("docker", "compose", "restart", "stage1")
    time.sleep(3)
    rss0 = rss_mib(sh("docker", "compose", "ps", "-q", "stage1"))  # свежий процесс без опросов — точка отсчёта памяти
    sh("docker", "compose", "stop", "stage1")  # этап 1 копит отставание, потом догоняет: так видна его скорость, а не скорость приёма
    rows, stage1 = [], []
    try:
        poll, n_votes = new_poll(), 0
        for n in REPLICAS:
            rps, cores, total, bad = ingest_run(poll, n)
            rows.append((n, rps, cores, bad))
            n_votes += total
        fill = max(1, round(n_votes / rows[-1][1]) - WARMUP - 1)  # секунд приёма до того же объёма на последнем числе реплик
        for k, w in enumerate(WORKERS):
            if k:  # своё отставание на каждое число процессов: новый опрос, этап 1 остановлен
                poll = new_poll()
                n_votes = ingest_run(poll, REPLICAS[-1], fill)[2]
            stage1.append((w, n_votes) + stage1_run(w, poll, n_votes))
    finally:
        sh("docker", "compose", "up", "-d", "--no-deps", "--scale", "ingest=2", "ingest")
        sh("docker", "compose", "start", "stage1")

    print(f"\nМашина: {machine()}")
    print(f"Приём: генератор в сети compose -> ingest:8000, {PROCS * CONNS} соединений keep-alive на реплику, "
          f"CPU из cgroup за {SECONDS} с после {WARMUP} с прогрева")
    print(f"{'реплик':>7} {'голосов/с':>10} {'ядер':>6} {'на ядро':>8} {'не 204':>7}")
    for n, rps, cores, bad in rows:
        print(f"{n:>7} {rps:>10.0f} {cores:>6.2f} {rps / cores:>8.0f} {bad:>7}")
    if any(r[3] for r in rows):
        print("ВНИМАНИЕ: не все ответы 204")
    print("Этап 1: догоняет отставание опроса (с пересылкой в votes_by_ip); скорость (по votes_by_ip) и CPU (ядер, cgroup) — от 10 % до 90 % отставания; "
          "Redpanda — один брокер стенда, --smp 1")
    print(f"{'процессов':>9} {'голосов':>8} {'голосов/с':>10} {'на процесс':>11} {'CPU этапа 1':>12} {'CPU Redpanda':>13}")
    for w, n, rate, cores, rp_cores, *_ in stage1:
        print(f"{w:>9} {n:>8} {rate:>10.0f} {rate / w:>11.0f} {cores:>12.2f} {rp_cores:>13.2f}")
    w, n_votes, _, _, _, rss1, t_final, res = stage1[0]
    print(f"Память этапа 1 ({w} процесс): RSS {rss0:.0f} -> {rss1:.0f} МиБ, "
          f"{(rss1 - rss0) * 2**20 * w / n_votes:.0f} Б на уникальный voter_id (вместе с буферами Kafka)")
    print(f"Этап 2: от закрытия окна до final {t_final:.1f} с по {n_votes} голосам (включая delivery.timeout 3 с); "
          f"received {res['received']}, total {res['total']}, counted {res['counted']}")


if __name__ == "__main__":
    gen(sys.argv[2], int(sys.argv[3])) if sys.argv[1:2] == ["gen"] else main()
