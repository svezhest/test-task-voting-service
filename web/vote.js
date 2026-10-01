// Страница опроса: /p/{poll_id}. Конфиг — /p/{poll_id}/config.json, голос — POST /api/vote.

var pollId = decodeURIComponent(location.pathname.split('/')[2] || '');
var votedKey = 'voted:' + pollId; // JSON-массив выбранных вариантов

function $(id) { return document.getElementById(id); }
function status(text) { $('status').textContent = text; }
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

// crypto.randomUUID есть только в secure context (https), а в локальной сети страница открыта по http.
function uuid4() {
  if (crypto.randomUUID) return crypto.randomUUID();
  var b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = b[6] & 0x0f | 0x40; b[8] = b[8] & 0x3f | 0x80;
  var h = Array.from(b, function (x) { return x.toString(16).padStart(2, '0'); }).join('');
  return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
}

// voter_id: UUID v4 в localStorage и в своей cookie. Берём тот, что уже есть.
var cookie = document.cookie.match(/(?:^|; )voter_id=([^;]+)/);
var voterId = lsGet('voter_id') || (cookie && cookie[1]) || uuid4();
lsSet('voter_id', voterId);
// STUB: срок жизни cookie в docs не задан, взят 1 год.
document.cookie = 'voter_id=' + voterId + '; path=/; max-age=31536000; SameSite=Lax' +
  (location.protocol === 'https:' ? '; Secure' : '');

// STUB: если подсчёт отпечатка упал, шлём пустой fp — поведение в docs не задано.
var fpPromise = getFp().catch(function () { return {}; });

// ?debug=1: показать составляющие отпечатка с короткими хэшами — чтобы сравнивать обычное окно и инкогнито.
if (/[?&]debug=1/.test(location.search)) fpPromise.then(function (fp) {
  function h(s) { var x = 2166136261; for (var i = 0; i < s.length; i++) x = Math.imul(x ^ s.charCodeAt(i), 16777619); return (x >>> 0).toString(16).padStart(8, '0').slice(0, 6); }
  var pre = document.createElement('pre');
  pre.className = 'debug';
  pre.textContent = 'all ' + h(JSON.stringify(fp)) + '\n' + Object.keys(fp).sort().map(function (k) {
    var v = String(JSON.stringify(fp[k])); return h(v) + '  ' + k + ' = ' + v.slice(0, 80);
  }).join('\n');
  document.querySelector('main').append(pre);
});

// Экран вместо формы: крупный заголовок, выбранные варианты (если есть), пояснение.
// Без заголовка — «Опрос не найден», и вопрос тоже прячем.
function show(title, note, choice) {
  if (!title) { $('q').hidden = true; title = 'Опрос не найден'; note = 'Проверьте ссылку.'; }
  $('form').hidden = true;
  status('');
  $('title').textContent = title;
  $('note').textContent = note;
  $('chosen').hidden = !choice;
  (choice || []).forEach(function (c) {
    var li = document.createElement('li');
    li.textContent = c;
    $('choice').append(li);
  });
  $('msg').hidden = false;
}

// Окно (window_start, window_end, grace_s) для голоса на клиенте не проверяем — это решает приём (410).
// По окну только ограничиваем повторы отправки.
var pollConfig = null;
fetch('/p/' + encodeURIComponent(pollId) + '/config.json')
  .then(function (r) { if (!r.ok) throw r.status; return r.json(); })
  .then(function (cfg) {
    pollConfig = cfg;
    $('q').textContent = cfg.question;
    document.title = cfg.question;
    var saved;
    try { saved = JSON.parse(lsGet(votedKey)); } catch (e) {}
    if (saved) {
      return show('Голос принят', 'Вы уже голосовали в этом опросе. Страницу можно закрыть.', saved);
    }
    $('hint').textContent = cfg.type === 'multi' ? 'Можно выбрать несколько вариантов' : 'Выберите один вариант';
    cfg.options.forEach(function (o) {
      var label = document.createElement('label');
      label.append(Object.assign(document.createElement('input'),
        { type: cfg.type === 'multi' ? 'checkbox' : 'radio', name: 'opt', value: o.idx }), o.label);
      $('opts').append(label);
    });
    $('form').hidden = false;
    $('f').disabled = false;
  })
  .catch(function (code) {
    if (code === 404) return show();
    $('q').hidden = true;
    show('Не удалось загрузить опрос', 'Проверьте связь и обновите страницу.');
  });

$('form').onsubmit = async function (e) {
  e.preventDefault();
  var checked = Array.from(document.querySelectorAll('input[name=opt]:checked'));
  if (!checked.length) return status('Выберите вариант.');
  var options = checked.map(function (i) { return +i.value; });
  var labels = checked.map(function (i) { return i.parentNode.textContent; });
  $('f').disabled = true;
  status('Отправляем…');
  var body = JSON.stringify({ poll_id: pollId, options: options, voter_id: voterId, fp: await fpPromise });

  // Сетевая ошибка и любой 5xx (503 — приём перегружен, 502/504 — упал узел приёма, 52x/530 — Cloudflare не достучался
  // до стенда): повтор с тем же телом и voter_id, экспоненциальная задержка с полным разбросом.
  // Повторяем, пока голосование может идти: до конца окна с задержкой (+10 с на расхождение часов), но не меньше 15 с.
  // Дальше повторять незачем — голос всё равно не примут.
  var windowCloses = Date.parse(pollConfig.window_end) + pollConfig.grace_s * 1000 + 10000;
  var retryUntil = Math.max(windowCloses, Date.now() + 15000);
  // STUB: база 0.5 с и потолок 30 с в docs не заданы.
  var code;
  for (var n = 0; ; n++) {
    try {
      code = (await fetch('/api/vote', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true
      })).status;
    } catch (err) {
      code = 0;
    }
    if (code !== 0 && code < 500) break;
    if (Date.now() > retryUntil) break;
    status(code === 503 ? 'Сервер занят, пробуем ещё раз…' : 'Нет связи с сервером голосования, пробуем ещё раз…');
    var pause = Math.random() * Math.min(30000, 500 * Math.pow(2, n));
    await new Promise(function (r) { setTimeout(r, Math.min(pause, Math.max(0, retryUntil - Date.now()))); });
  }

  if (code === 204) {
    lsSet(votedKey, JSON.stringify(labels));
    // Небольшой залп цветами темы; при prefers-reduced-motion библиотека ничего не рисует.
    var css = getComputedStyle(document.documentElement);
    if (window.confetti) confetti({ particleCount: 40, spread: 60, origin: { y: 0.7 }, disableForReducedMotion: true,
      colors: ['--accent', '--fg', '--muted'].map(function (v) { return css.getPropertyValue(v).trim(); }) });
    return show('Голос принят', 'Страницу можно закрыть.', labels);
  }
  if (code === 410) return show('Голосование не идёт', 'Приём голосов ещё не открыт или уже закрыт.');
  if (code === 404) return show();
  // 422 и прочие ошибки — даём выбрать и отправить снова.
  // STUB: 400 в docs для клиента не описан: без повтора, форма снова доступна.
  $('f').disabled = false;
  if (code === 422) return status('Этот выбор не принят. Выберите заново.');
  if (code === 0 || code >= 500) return status('Голос не отправлен: нет связи с сервером голосования. Попробуйте ещё раз.');
  status('Не получилось отправить. Попробуйте ещё раз.');
};
