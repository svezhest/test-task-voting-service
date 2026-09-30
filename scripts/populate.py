"""Демо-данные для стенда: черновики, запланированный, завершённые и идущий опросы. Только HTTP-API."""
import asyncio, math, os, random, sys, time, uuid
from datetime import datetime, timedelta, timezone

import httpx

ADMIN, VOTE = "http://localhost:8091", "http://localhost:8090/api/vote"
TOKEN = os.environ.get("ADMIN_TOKEN", "dev-token")

DRAFTS = [  # (дней от сегодня, вопрос, тип, варианты)
    (-6, "Лучшая песня вечера?", "single", ["«Кукушка»", "«Звезда по имени Солнце»", "«Группа крови»"]),
    (-1, "Согласны ли вы с решением жюри?", "single", ["Да", "Нет", "Затрудняюсь ответить"]),
    (1, "Какую тему обсудить в следующем выпуске?", "multi", ["ЖКХ", "Цены на продукты", "Дороги", "Здравоохранение"]),
    (4, "Какой вид спорта показывать чаще?", "multi", ["Футбол", "Хоккей", "Биатлон", "Фигурное катание", "Теннис"]),
    (91, "Где вы встретите Новый год?", "single", ["Дома", "В гостях", "В путешествии", "На работе"]),
]
SCHEDULED = ("Кто пройдёт в следующий тур шоу талантов?", "single", ["Ансамбль «Берёзка»", "Иллюзионист Марк", "Дуэт «Север»"])
FINISHED = [
    ("Кто должен победить в финале «Голоса»?", "single", ["Анна Петрова", "Илья Смирнов", "Дарья Ковалёва"]),
    ("Какие передачи канала вы смотрите?", "multi", ["Новости", "Сериалы", "Ток-шоу", "Спорт", "Кулинарные шоу"]),
    ("Нужно ли вернуть зимнее время?", "single", ["Да", "Нет"]),
]
RUNNING = ("Какой фильм показать в пятницу вечером?", "single",
           ["«Бриллиантовая рука»", "«Иван Васильевич меняет профессию»", "«Джентльмены удачи»", "«Москва слезам не верит»"])

DEVICES = [  # (os, engine, browser, version, модели, dpr)
    ("iOS", "WebKit", "Safari", "26", [None], [3, 2]),
    ("iOS", "WebKit", "Telegram", "26", [None], [3]),
    ("Android", "Blink", "Chrome", "140", ["SM-A546E", "SM-S921B", "23129RAA4G", "Pixel 8", "RMX3710", "2201117TY"], [2.625, 2.75, 3]),
    ("Android", "Blink", "YaBrowser", "25", ["SM-A155F", "23053RN02Y", "V2207"], [2, 2.625]),
    ("Android", "Blink", "SamsungBrowser", "28", ["SM-A356E", "SM-S918B"], [2.625, 3]),
    ("Windows", "Blink", "Chrome", "141", [None], [1, 1.25, 1.5]),
    ("macOS", "WebKit", "Safari", "26", [None], [2]),
]
TZ = ["Europe/Moscow"] * 6 + ["Asia/Yekaterinburg", "Asia/Novosibirsk", "Europe/Samara", "Asia/Krasnoyarsk"]


def device():
    os_, engine, browser, version, models, dprs = random.choice(DEVICES)
    fp = {"os": os_, "engine": engine, "browser": browser, "version": version, "model": random.choice(models),
          "display": {"dpr": random.choice(dprs), "colorDepth": 24, "gamut": random.choice(["srgb", "p3"]),
                      "dynamicRange": random.choice(["standard", "high"])},
          "language": random.choice(["ru"] * 9 + ["en"]), "locale": "ru-RU", "calendar": "gregory", "hourCycle": "h23",
          "timezone": random.choice(TZ),
          "media": {"colorScheme": random.choice(["light", "dark"]), "reducedMotion": random.choice(["no-preference"] * 9 + ["reduce"]),
                    "contrast": "no-preference", "invertedColors": "none", "forcedColors": "none", "reducedTransparency": "no-preference"}}
    if os_ == "iOS":
        fp["fontSize"] = random.choice(["17px"] * 5 + ["19px", "21px"])
    return fp


def ip():
    return f"{random.choice([5, 31, 46, 77, 85, 91, 95, 176, 178, 188, 217])}.{random.randrange(256)}.{random.randrange(256)}.{random.randrange(1, 255)}"


def arrival(until):  # логнормальная кривая из architecture.md: медиана 14 с, σ = 0.5
    while (t := random.lognormvariate(math.log(14), 0.5)) > until:
        pass
    return t


def plan(poll, until, honest):
    """Список (секунда от начала окна, тело голоса, IP)."""
    n = len(poll["options"])
    w = [0.55 ** i for i in range(n)]
    random.shuffle(w)  # явный лидер — вариант с весом 1

    def choice():
        if poll["type"] == "single":
            return random.choices(range(n), w)
        return sorted({*random.choices(range(n), w, k=random.choice([1, 1, 2, 3]))})

    def vote(t, vid, fp, addr, opts=None):
        return t, {"poll_id": poll["id"], "options": opts or choice(), "voter_id": vid, "fp": fp}, addr

    votes, cgnat = [], [ip() for _ in range(honest // 30)]
    for _ in range(honest):  # честные зрители; каждый десятый — за общим IP мобильного оператора
        v = vote(arrival(until), str(uuid.uuid4()), device(), random.choice(cgnat) if random.random() < 0.1 else ip())
        votes.append(v)
        if random.random() < 0.05:  # повтор из того же браузера, но уже с LTE
            votes.append(vote(min(until, v[0] + random.uniform(2, 15)), v[1]["voter_id"], v[1]["fp"], ip()))
    cheat_fp, cheat_ip, target = device(), ip(), [w.index(min(w))]
    for i in range(25):  # накрутчик в инкогнито: новое окно каждые ~1.5 с, отпечаток и IP те же
        votes.append(vote(3 + i * 1.5, str(uuid.uuid4()), cheat_fp, cheat_ip, target))
    ipad, school = device(), ip()
    for _ in range(100):  # класс одинаковых планшетов за одним NAT
        votes.append(vote(arrival(until), str(uuid.uuid4()), ipad, school))
    return votes


async def send(client, t0, t, body, addr):
    await asyncio.sleep(max(0, t0 + t - time.time()))
    for _ in range(30):  # приём могут перезапускать: 5xx и обрывы повторяем
        try:
            r = await client.post(VOTE, json=body, headers={"X-Forwarded-For": addr})
            if r.status_code < 500 and r.status_code != 404:  # 404 — приём ещё не перечитал опросы
                return r.status_code
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)


async def create(admin, question, type_, options, start, end, grace, activate):
    r = await admin.post("/admin/polls", json={"question": question, "type": type_, "options": options,
                                               "window_start": start.isoformat(), "window_end": end.isoformat(), "grace_s": grace})
    r.raise_for_status()
    poll = r.json()
    if activate:
        (await admin.post(f"/admin/polls/{poll['id']}/activate")).raise_for_status()
    return poll


async def wait_final(admin, poll):
    while (await admin.get(f"/admin/polls/{poll['id']}/results")).json()["status"] != "final":
        await asyncio.sleep(1)
    return (await admin.get(f"/admin/polls/{poll['id']}/analytics")).json()["funnel"]


async def main():
    async with httpx.AsyncClient(base_url=ADMIN, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10) as admin, \
               httpx.AsyncClient(timeout=httpx.Timeout(10, pool=None), limits=httpx.Limits(max_connections=50)) as voters:
        (await admin.get("/admin/polls")).raise_for_status()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        today = now.replace(hour=17, minute=30, second=0)  # 20:30 по Москве
        for days, *q in DRAFTS:
            d = today + timedelta(days=days)
            await create(admin, *q, d, d + timedelta(minutes=1), 30, False)
        s = now + timedelta(minutes=5)
        await create(admin, *SCHEDULED, s, s + timedelta(minutes=1), 30, True)

        start = now + timedelta(seconds=3)
        finished = [await create(admin, *q, start, start + timedelta(seconds=45), 5, True) for q in FINISHED]
        running = await create(admin, *RUNNING, start, start + timedelta(minutes=10), 30, True)
        votes = [(p, v) for p in finished for v in plan(p, 42, random.randint(1000, 3000))]
        votes += [(running, v) for v in plan(running, 40, random.randint(600, 1000))]
        print(f"Отправляю {len(votes)} голосов в 4 опроса по логнормальной кривой…")
        codes = await asyncio.gather(*(send(voters, start.timestamp(), *v) for _, v in votes))
        print("Ответы приёма:", {c: codes.count(c) for c in set(codes)})
        print("Жду итогов (status = final)…")
        funnels = await asyncio.wait_for(asyncio.gather(*(wait_final(admin, p) for p in finished)), 120)

    print(f"\nСоздано: {len(DRAFTS)} черновиков, 1 запланированный (начнётся в {s:%H:%M} UTC), "
          f"{len(finished)} завершённых, 1 идущий (до {start + timedelta(minutes=10):%H:%M} UTC).")
    for p, f in zip(finished, funnels):
        print(f"  «{p['question']}»: пришло {f['received']}, уникальных {f['unique_voters']}, засчитано {f['counted']}, отсев {f['rejected']}")
    print(f"  Идёт: «{running['question']}» — http://localhost:8090/p/{running['id']}")
    print("Админка: http://localhost:8091/manage/")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.TransportError as e:
        sys.exit(f"Стенд недоступен ({ADMIN}): {e!r}. Поднимите его: make up")
    except httpx.HTTPStatusError as e:
        sys.exit(f"Админка ответила {e.response.status_code} на {e.request.url}: {e.response.text[:300]}")
    except asyncio.TimeoutError:
        sys.exit("Итоги не посчитались за 2 минуты — проверьте stage1/stage2 (docker compose logs).")
