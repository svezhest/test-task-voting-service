# Отпечаток браузера

## Принцип: один белый список для всех браузеров

В отпечаток входят только признаки, которые **ни один браузер не шумит и которые не меняются между обычным и приватным окном**. Список один для всех браузеров, без исключений по семейству.

Почему так. Сначала мы шли от обратного: брали всё и исключали то, что портит конкретный браузер. Каждое такое исключение потом приходилось распространять на всех:
- canvas сначала выкидывали только в Safari, потом во всём Chromium;
- «только первый язык» было правилом Brave, а потом Cromite на живом телефоне в инкогнито урезал языки и сдвинул размер экрана.

Защиты от отпечатков расползаются по браузерам быстрее, чем пополняется таблица исключений. Белый список закрывает этот класс ошибок сразу, и код получается короче.

Цена: отпечаток грубее. Основную работу по дедупликации делает статистика — лимит `N`, поправка `R` и потолок на IP (см. [architecture_deduplication.md](architecture_deduplication.md)).

## Белый список

| Признак | Почему устойчив |
|---|---|
| ОС, движок, браузер, мажорная версия (у Safari — `Version/26.x`), метка in-app браузера | Берутся из UA и не зависят от режима окна |
| Модель устройства (`userAgentData.getHighEntropyValues`), если браузер её отдаёт | Кто не отдаёт, не отдаёт никогда (например, Cromite) |
| `devicePixelRatio`, глубина цвета, `color-gamut`, `dynamic-range` | Свойства экрана, их не шумят |
| Первый язык, локаль, календарь, формат часов, часовой пояс | Браузеры урезают только хвост списка языков |
| Медиазапросы: `prefers-color-scheme`, `reduced-motion`, `contrast`, `inverted-colors`, `forced-colors`, `reduced-transparency` | Это пользовательские настройки |
| Размер шрифта `-apple-system-body` | Настройка iOS; у остальных браузеров это константа |

**Не берём:** canvas, audio, WebGL, шрифты, размеры экрана, число ядер, память, тип защиты, квоту storage, размеры и координаты окна, тайминги, `navigator.connection`.

**Решение автора: тема (`prefers-color-scheme`) входит в отпечаток.** Она добавляет до 1 бита. Переключиться по расписанию за окно в 1–2 минуты она почти не может. Если это всё же случится, честный человек не пострадает: он голосует один раз.

## Что нашли на живых устройствах

- **Cromite** (Chromium с защитой от отпечатков) на Android: в инкогнито подрезает список языков и на несколько пикселей меняет `screen.width` и `screen.height`. WebGL выключен, модель не отдаётся, canvas шумит при каждом чтении. Отпечаток по белому списку при этом стабилен.
- **LibreWolf** (Firefox с RFP), по исследованию исходников: подменяет UA, часовой пояс и язык одинаково в обоих режимах. Полосы в canvas — это заглушка RFP: 32 случайных байта, повторённые по буферу. Руками пока не проверен.

## Проверка на устройствах

Страница с `?debug=1` показывает составляющие отпечатка с короткими хэшами. Открываем её в обычном и в приватном окне и сравниваем. Отпечаток должен совпасть:
1. в обычном окне;
2. в приватном окне;
3. в новой приватной сессии;
4. после перезапуска браузера;
5. с VPN.

Проверено: Chrome (headless), Cromite на Android.
Проверить ещё: iPhone с Safari, Samsung Internet, Firefox, Brave, LibreWolf, встроенные браузеры Telegram и VK.

## Источники

- [WebKit: Safari 26.0](https://webkit.org/blog/17333/webkit-features-in-safari-26-0/)
- [WebKit: Private Browsing 2.0](https://webkit.org/blog/14510/private-browsing-2-0/)
- [51Degrees: заморозка UA в iOS 26](https://51degrees.com/blog/apple-ios26-safari26-user-agent-string-device-detection)
- [Mozilla: защита от отпечатков в Firefox](https://support.mozilla.org/en-US/kb/firefox-protection-against-fingerprinting)
- [Firefox: `GeneratePlaceholderCanvasData.h`](https://github.com/mozilla-firefox/firefox/blob/main/dom/canvas/GeneratePlaceholderCanvasData.h)
- [Brave wiki: Fingerprinting Protections](https://github.com/brave/brave-browser/wiki/Fingerprinting-Protections)
- [Cromite](https://github.com/uazo/cromite)
- [CreepJS](https://github.com/abrahamjuliot/creepjs)
- [Laperdrix et al., Browser Fingerprinting: A Survey](https://arxiv.org/abs/1905.01051)
