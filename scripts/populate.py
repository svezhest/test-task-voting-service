"""Демо-данные для стенда: черновики, запланированный, завершённые и идущий опросы. Только HTTP-API."""
import asyncio, math, os, random, sys, time, uuid
from datetime import datetime, timedelta, timezone

import httpx

ADMIN = "http://localhost:8091"
VOTE = ADMIN + "/api/vote"  # через админский вход: только там приём верит X-Forwarded-For (IP «зрителей»)
TOKEN = os.environ.get("ADMIN_TOKEN", "dev-token")

DRAFTS = [  # (дней от сегодня, вопрос, тип, варианты)
    (-6, "Сколько часов в день вы проводите в телефоне?", "single", ["Меньше 2", "2–4", "4–6", "Больше 6"]),
    (-1, "Пользуетесь ли вы ИИ-помощниками?", "single", ["Каждый день", "Иногда", "Пробовал(а), не зашло", "Ещё нет"]),
    (1, "Что вы делаете, чтобы меньше уставать?", "multi", ["Спорт", "Прогулки", "Меньше соцсетей", "Режим сна", "Хобби"]),
    (4, "Какой отпуск вам ближе?", "single", ["Море", "Горы", "Город", "Дома с книгой"]),
    (91, "Какую привычку вы хотите завести в новом году?", "multi", ["Спорт", "Чтение", "Новый язык", "Готовить дома", "Высыпаться"]),
]
SCHEDULED = ("Удалёнка, офис или гибрид?", "single", ["Удалённо", "В офисе", "Гибрид"])
FINISHED = [
    ("Нужна ли четырёхдневная рабочая неделя?", "single", ["Да", "Нет", "Только по желанию"]),
    ("Как вы добираетесь на работу или учёбу?", "multi", ["Пешком", "Велосипед или самокат", "Общественный транспорт", "Автомобиль", "Работаю из дома"]),
    ("Кошки или собаки?", "single", ["Кошки", "Собаки", "И те, и другие", "Никого"]),
]
RUNNING = ("Что смотреть в пятницу вечером?", "single",
           ["Комедию", "Детектив", "Документальный фильм", "Мультфильм"])

DEVICES = [  # (os, engine, browser, version, модели, dpr)
    ("iOS", "WebKit", "Safari", "26", [None], [3, 2]),
    ("iOS", "WebKit", "Telegram", "26", [None], [3]),
    ("Android", "Blink", "Chrome", "140", ["SM-A546E", "SM-S921B", "23129RAA4G", "Pixel 8", "RMX3710", "2201117TY"], [2.625, 2.75, 3]),
    ("Android", "Blink", "YaBrowser", "25", ["SM-A155F", "23053RN02Y", "V2207"], [2, 2.625]),
    ("Android", "Blink", "SamsungBrowser", "28", ["SM-A356E", "SM-S918B"], [2.625, 3]),
    ("Windows", "Blink", "Chrome", "141", [None], [1, 1.25, 1.5]),
    ("macOS", "WebKit", "Safari", "26", [None], [2]),
]
TIMEZONES = ["Europe/Moscow"] * 6 + ["Asia/Yekaterinburg", "Asia/Novosibirsk", "Europe/Samara", "Asia/Krasnoyarsk"]
FIRST_OCTETS = [5, 31, 46, 77, 85, 91, 95, 176, 178, 188, 217]
MEDIAN_ARRIVAL_S, ARRIVAL_SIGMA = 14, 0.5  # логнормальная кривая из architecture.md


def random_fingerprint():
    os_name, engine, browser, version, models, dprs = random.choice(DEVICES)
    fingerprint = {
        "os": os_name, "engine": engine, "browser": browser, "version": version, "model": random.choice(models),
        "display": {"dpr": random.choice(dprs), "colorDepth": 24, "gamut": random.choice(["srgb", "p3"]),
                    "dynamicRange": random.choice(["standard", "high"])},
        "language": random.choice(["ru"] * 9 + ["en"]), "locale": "ru-RU", "calendar": "gregory", "hourCycle": "h23",
        "timezone": random.choice(TIMEZONES),
        "media": {"colorScheme": random.choice(["light", "dark"]), "reducedMotion": random.choice(["no-preference"] * 9 + ["reduce"]),
                  "contrast": "no-preference", "invertedColors": "none", "forcedColors": "none", "reducedTransparency": "no-preference"},
    }
    if os_name == "iOS":
        fingerprint["fontSize"] = random.choice(["17px"] * 5 + ["19px", "21px"])
    return fingerprint


def random_ip():
    return f"{random.choice(FIRST_OCTETS)}.{random.randrange(256)}.{random.randrange(256)}.{random.randrange(1, 255)}"


def arrival_second(until):
    while True:
        second = random.lognormvariate(math.log(MEDIAN_ARRIVAL_S), ARRIVAL_SIGMA)
        if second <= until:
            return second


def plan_votes(poll, until, honest_count):
    """Список (секунда от начала окна, тело голоса, IP)."""
    option_count = len(poll["options"])
    weights = [0.55 ** idx for idx in range(option_count)]
    random.shuffle(weights)  # явный лидер — вариант с весом 1
    least_popular = weights.index(min(weights))

    def pick_options():
        if poll["type"] == "single":
            return random.choices(range(option_count), weights)
        return sorted({*random.choices(range(option_count), weights, k=random.choice([1, 1, 2, 3]))})

    def planned_vote(second, voter_id, fingerprint, ip, options=None):
        if options is None:
            options = pick_options()
        return second, {"poll_id": poll["id"], "options": options, "voter_id": voter_id, "fp": fingerprint}, ip

    votes = []
    carrier_ips = [random_ip() for _ in range(honest_count // 30)]
    for _ in range(honest_count):
        second = arrival_second(until)
        voter_id = str(uuid.uuid4())
        fingerprint = random_fingerprint()
        if random.random() < 0.1:  # каждый десятый честный зритель — за общим IP мобильного оператора
            ip = random.choice(carrier_ips)
        else:
            ip = random_ip()
        votes.append(planned_vote(second, voter_id, fingerprint, ip))
        if random.random() < 0.05:  # повтор из того же браузера, но уже с LTE
            repeat_second = min(until, second + random.uniform(2, 15))
            votes.append(planned_vote(repeat_second, voter_id, fingerprint, random_ip()))

    cheater_fingerprint, cheater_ip = random_fingerprint(), random_ip()
    for attempt in range(25):  # накрутчик в инкогнито: новое окно каждые ~1.5 с, отпечаток и IP те же
        votes.append(planned_vote(3 + attempt * 1.5, str(uuid.uuid4()), cheater_fingerprint, cheater_ip, [least_popular]))

    tablet_fingerprint, school_ip = random_fingerprint(), random_ip()
    for _ in range(100):  # класс одинаковых планшетов за одним NAT
        votes.append(planned_vote(arrival_second(until), str(uuid.uuid4()), tablet_fingerprint, school_ip))
    return votes


async def send_vote(client, window_start_ts, second, body, ip):
    await asyncio.sleep(max(0, window_start_ts + second - time.time()))
    for _ in range(30):  # приём могут перезапускать: 5xx и обрывы повторяем
        try:
            response = await client.post(VOTE, json=body, headers={"X-Forwarded-For": ip})
            if response.status_code < 500 and response.status_code != 404:  # 404 — приём ещё не перечитал опросы
                return response.status_code
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)


async def create_poll(admin, question, poll_type, options, window_start, window_end, grace_s, activate):
    response = await admin.post("/admin/polls", json={
        "question": question, "type": poll_type, "options": options,
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(), "grace_s": grace_s,
    })
    response.raise_for_status()
    poll = response.json()
    if activate:
        (await admin.post(f"/admin/polls/{poll['id']}/activate")).raise_for_status()
    return poll


async def wait_for_funnel(admin, poll):
    while True:
        results = (await admin.get(f"/admin/polls/{poll['id']}/results")).json()
        if results["status"] == "final":
            break
        await asyncio.sleep(1)
    return (await admin.get(f"/admin/polls/{poll['id']}/analytics")).json()["funnel"]


async def main():
    async with httpx.AsyncClient(base_url=ADMIN, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10) as admin, \
               httpx.AsyncClient(timeout=httpx.Timeout(10, pool=None), limits=httpx.Limits(max_connections=50)) as voters:
        (await admin.get("/admin/polls")).raise_for_status()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        today_on_air = now.replace(hour=17, minute=30, second=0)  # 20:30 по Москве
        for days_from_today, *poll_fields in DRAFTS:
            draft_start = today_on_air + timedelta(days=days_from_today)
            await create_poll(admin, *poll_fields, draft_start, draft_start + timedelta(minutes=1), 30, False)
        scheduled_start = now + timedelta(minutes=5)
        await create_poll(admin, *SCHEDULED, scheduled_start, scheduled_start + timedelta(minutes=1), 30, True)

        start = now + timedelta(seconds=3)
        finished = [await create_poll(admin, *poll_fields, start, start + timedelta(seconds=45), 5, True) for poll_fields in FINISHED]
        running = await create_poll(admin, *RUNNING, start, start + timedelta(minutes=10), 30, True)
        votes = [vote for poll in finished for vote in plan_votes(poll, 42, random.randint(1000, 3000))]
        votes += plan_votes(running, 40, random.randint(600, 1000))
        print(f"Отправляю {len(votes)} голосов в 4 опроса по логнормальной кривой…")
        status_codes = await asyncio.gather(*(send_vote(voters, start.timestamp(), *vote) for vote in votes))
        print("Ответы приёма:", {code: status_codes.count(code) for code in set(status_codes)})
        print("Жду итогов (status = final)…")
        funnels = await asyncio.wait_for(asyncio.gather(*(wait_for_funnel(admin, poll) for poll in finished)), 120)

    print(f"\nСоздано: {len(DRAFTS)} черновиков, 1 запланированный (начнётся в {scheduled_start:%H:%M} UTC), "
          f"{len(finished)} завершённых, 1 идущий (до {start + timedelta(minutes=10):%H:%M} UTC).")
    for poll, funnel in zip(finished, funnels):
        print(f"  «{poll['question']}»: пришло {funnel['received']}, уникальных {funnel['unique_voters']}, "
              f"засчитано {funnel['counted']}, отсев {funnel['rejected']}")
    print(f"  Идёт: «{running['question']}» — http://localhost:8090/p/{running['id']}")
    print("Админка: http://localhost:8091/manage/")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.TransportError as error:
        sys.exit(f"Стенд недоступен ({ADMIN}): {error!r}. Поднимите его: make up")
    except httpx.HTTPStatusError as error:
        sys.exit(f"Админка ответила {error.response.status_code} на {error.request.url}: {error.response.text[:300]}")
    except asyncio.TimeoutError:
        sys.exit("Итоги не посчитались за 2 минуты — проверьте stage1/stage2 (docker compose logs).")
