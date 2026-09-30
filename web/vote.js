// Страница опроса: /p/{poll_id}. Конфиг — /p/{poll_id}/config.json, голос — POST /api/vote.

var pollId = decodeURIComponent(location.pathname.split('/')[2] || '');
var votedKey = 'voted:' + pollId;

function $(id) { return document.getElementById(id); }
function status(text) { $('status').textContent = text; }
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

// voter_id: UUID v4 в localStorage и в своей cookie. Берём тот, что уже есть.
var cookie = document.cookie.match(/(?:^|; )voter_id=([^;]+)/);
var voterId = lsGet('voter_id') || (cookie && cookie[1]) || crypto.randomUUID();
lsSet('voter_id', voterId);
// STUB: срок жизни cookie в docs не задан, взят 1 год.
document.cookie = 'voter_id=' + voterId + '; path=/; max-age=31536000; SameSite=Lax' +
  (location.protocol === 'https:' ? '; Secure' : '');

// STUB: если подсчёт отпечатка упал, шлём пустой fp — поведение в docs не задано.
var fpPromise = getFp().catch(function () { return {}; });

// STUB: окно (window_start, window_end, grace_s) на клиенте не проверяем — это решает приём (410).
fetch('/p/' + encodeURIComponent(pollId) + '/config.json')
  .then(function (r) { if (!r.ok) throw r.status; return r.json(); })
  .then(function (cfg) {
    $('q').textContent = cfg.question;
    document.title = cfg.question;
    cfg.options.slice().sort(function (a, b) { return a.idx - b.idx; }).forEach(function (o) {
      var label = document.createElement('label');
      var input = document.createElement('input');
      input.type = cfg.type === 'multi' ? 'checkbox' : 'radio';
      input.name = 'opt';
      input.value = o.idx;
      label.append(input, o.label);
      $('opts').append(label);
    });
    if (lsGet(votedKey)) return status('Вы уже проголосовали.');
    $('f').disabled = false;
  })
  .catch(function () { $('q').textContent = 'Опрос не найден'; });

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

$('form').onsubmit = async function (e) {
  e.preventDefault();
  var options = Array.from(document.querySelectorAll('input[name=opt]:checked'), function (i) { return +i.value; });
  if (!options.length) return status('Выберите вариант.');
  $('f').disabled = true;
  status('Отправляем…');
  var body = JSON.stringify({ poll_id: pollId, options: options, voter_id: voterId, fp: await fpPromise });

  // 503 и сетевая ошибка: повтор с экспоненциальной задержкой и полным разбросом, пока вкладка открыта.
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
    if (code !== 503 && code !== 0) break;
    status('Сервер занят, пробуем ещё раз…');
    await sleep(Math.random() * Math.min(30000, 500 * Math.pow(2, n)));
  }

  if (code === 204) {
    lsSet(votedKey, '1');
    return status('Голос принят. Спасибо!');
  }
  if (code === 410) return status('Голосование сейчас не идёт: окно закрыто.');
  if (code === 404) return status('Опрос не найден или не активен.');
  // 422 и прочие ошибки — даём выбрать и отправить снова.
  // STUB: 400, 5xx кроме 503 (например, 502 от nginx при упавшем приёме) в docs для клиента не описаны: без повтора.
  $('f').disabled = false;
  if (code === 422) return status('Неверный выбор варианта. Выберите заново.');
  status('Ошибка ' + code + '. Попробуйте ещё раз.');
};
