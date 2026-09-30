# Отпечаток браузера

## Принцип

В ключ дедупликации (`fp_stable`) входит только то, что **совпадает в обычном и приватном окне** одного браузера и между приватными сессиями. Какие признаки исключать и как их нормализовать, решаем **по семейству браузера**, а не по обнаруженному режиму. Иначе сам режим попал бы в отпечаток, и инкогнито давало бы новый ключ.

Скрипт отпечатка лежит на нашем домене. В обычном режиме Safari 26 зашумляет и лишает cookie и localStorage только скрипты из списка известных трекеров, а свой скрипт с первого домена работает честно.

## Что делают браузеры

| Браузер | Что зашумлено или подменено | Обычный ↔ приватный |
|---|---|---|
| Chrome, Android Chrome | Ничего из отпечатка | Стабильно (кроме квоты storage) |
| Safari 17+ / 26, приватный режим | Canvas, WebGL-пиксели, audio. Экран iPhone сводится к 4 размерам. Шум новый на каждую вкладку | Canvas, audio и экран различаются между режимами |
| Safari 26, обычный режим | Только скрипты из списка трекеров. UA заморожен (`18_6`), `hardwareConcurrency` — 4 или 8 | — |
| Firefox, приватный режим (FPP) | Canvas (детерминированно в сессии), шрифты, `hardwareConcurrency` → 4/8, `maxTouchPoints`, `screen.avail*`, Math | Эти признаки различаются |
| Firefox RFP, LibreWolf, Tor, Mullvad | Canvas → заглушка, UA, часовой пояс UTC, язык en-US, экран = окно | Одинаково в обоих режимах |
| Brave | Почти всё, зерно «сессия × сайт». У приватного окна своё зерно | Стабильны только грубые признаки |

**Полосы в canvas LibreWolf** — это заглушка RFP: 32 случайных байта, повторённые по всему буферу. Они новые при каждом чтении, об устройстве ничего не говорят. Устойчив только сам факт «включена RFP-заглушка».

## Состав `fp_stable`

| Признак | Где берём | Нормализация и исключения |
|---|---|---|
| Семейство ОС, движок, браузер, мажорная версия; метки `navigator.brave`, Samsung Internet, in-app | Везде | На iOS версия ОС заморожена, берём `Version/26.x` |
| Модель устройства (`userAgentData.getHighEntropyValues`) | Chromium на Android | Главный источник энтропии на Android |
| Экран: `min(w,h)`, `max(w,h)`, DPR | Chrome | iPhone: свести к 4 размерам во всех режимах. iPad и Mac Safari, RFP, Brave: не брать |
| Глубина цвета, `color-gamut`, `dynamic-range` | Везде | — |
| Языки, `Intl` (локаль, календарь, формат часов), часовой пояс | Везде | Brave: только первый язык |
| `hardwareConcurrency`, `deviceMemory` | Везде, кроме Brave | Firefox: свести к 4/8 |
| WebGL: vendor/renderer, лимиты, расширения (без чтения пикселей) | Chrome, Firefox | Brave, RFP: не брать |
| Хэш canvas 2D | Только Chromium без Brave | Safari, Firefox, Brave, RFP: не брать |
| Audio (`OfflineAudioContext`) | Только Chrome | Остальные: не брать |
| Шрифты | Chrome; Firefox — только системные | Brave: не брать |
| Медиазапросы: `reduced-motion`, `contrast`, `inverted-colors`, `forced-colors`, `reduced-transparency` | Везде | `prefers-color-scheme` не брать: тема может переключаться по расписанию |
| Размер шрифта iOS (`-apple-system-body`) | Safari | Стабильность не проверена |
| Тип защиты: нет / RFP-заглушка / Brave / расширение | Только эти классы | Для Safari и Firefox не брать: там тип защиты выдаёт режим |

**Не брать никогда:** квоту storage, наличие cookie и storage, размеры окна, координаты окна, тайминги, `navigator.connection`, батарею.

## Как распознать тип защиты

Рисуем эталон: шахматку 8×8 из известных цветов, без сглаживания. Шахматка нужна по двум причинам: Firefox не трогает однотонные картинки, а Safari гасит шум в однотонных областях. Затем читаем пиксели и сравниваем побайтно:
- картинка не зависит от нарисованного и повторяется с периодом 8 пикселей → RFP-заглушка;
- все байты `0xFF` → белая заглушка;
- при каждом чтении другой плотный шум → расширение (например, CanvasBlocker);
- совпадает с эталоном → шума нет.

Двойное чтение само по себе ловит только шум, который меняется при каждом вызове. Safari, Firefox FPP и Brave внутри страницы детерминированы, поэтому для них canvas просто исключаем по семейству браузера.

## Сколько это различает

- **Android Chrome:** модель, экран, WebGL, canvas — отпечаток близок к уникальному в пределах одного IP.
- **iPhone Safari:** после исключений остаётся грубый класс устройства, по оценке ~8–12 бит (не измерено). За мобильным CGNAT одинаковые iPhone будут совпадать. Это учтено адаптивным лимитом `N` и поправкой `R` (см. [architecture_deduplication.md](architecture_deduplication.md)).

Данные об энтропии в исследованиях — 2016–2018 годов, до заморозки UA и нынешних защит. Для нас это ориентир, а не измерение.

## Ручная проверка

На iPhone (iOS 26 Safari), Android (Chrome, Samsung Internet), Firefox, Brave, LibreWolf, а также во встроенных браузерах Telegram и VK снять `fp_stable`:
1. обычное окно;
2. приватное окно;
3. вторая вкладка в той же приватной сессии;
4. новая приватная сессия;
5. после перезапуска браузера;
6. с VPN.

`fp_stable` должен совпасть во всех шести случаях. Отдельно проверить:
- в Safari: в консоли Web Inspector нет записей о Script Tracking Privacy для нашего скрипта;
- на iPhone: сведение экрана к 4 размерам и размер шрифта `-apple-system-body`;
- в Brave и Samsung Internet: отдаётся ли модель через `userAgentData`.

## Источники

- [WebKit: Safari 26.0](https://webkit.org/blog/17333/webkit-features-in-safari-26-0/)
- [WebKit: Private Browsing 2.0](https://webkit.org/blog/14510/private-browsing-2-0/)
- [Разбор настроек защиты Safari 26 (lapcatsoftware)](https://lapcatsoftware.com/articles/2025/9/4.html)
- [51Degrees: заморозка UA в iOS 26](https://51degrees.com/blog/apple-ios26-safari26-user-agent-string-device-detection)
- [Mozilla: защита от отпечатков в Firefox](https://support.mozilla.org/en-US/kb/firefox-protection-against-fingerprinting)
- [Firefox: `GeneratePlaceholderCanvasData.h`](https://github.com/mozilla-firefox/firefox/blob/main/dom/canvas/GeneratePlaceholderCanvasData.h)
- [LibreWolf FAQ](https://librewolf.net/docs/faq/)
- [Brave wiki: Fingerprinting Protections](https://github.com/brave/brave-browser/wiki/Fingerprinting-Protections)
- [FingerprintJS (open source)](https://github.com/fingerprintjs/fingerprintjs)
- [CreepJS](https://github.com/abrahamjuliot/creepjs)
- [CanvasBlocker: настройки по умолчанию](https://github.com/kkapsner/CanvasBlocker/blob/master/lib/settingDefinitions.js)
- [Laperdrix et al., Browser Fingerprinting: A Survey](https://arxiv.org/abs/1905.01051)
