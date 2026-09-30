'use strict';
const $ = s => document.querySelector(s);
const main = $('#main');
const KEY = 'admin_token';
let token = localStorage.getItem(KEY) || '';
let gen = 0;      // номер текущего экрана: ответы от ушедших экранов не рисуем
let timer = null;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const num = n => n == null ? '—' : Number(n).toLocaleString('ru-RU');
const short = n => new Intl.NumberFormat('ru-RU', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
const pct = x => x == null ? '—' : (x * 100).toFixed(1).replace('.', ',') + '%';
const dt = s => new Date(s).toLocaleString('ru-RU');
const when = s => new Date(s).toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
const local = d => new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); // для datetime-local
const STATUS = { draft: 'Черновик', active: 'Идёт голосование', counting: 'Подсчёт', final: 'Готово' };
const TYPE = { single: 'Один вариант ответа', multi: 'Несколько вариантов ответа' };
const badge = s => `<span class="badge s-${esc(s)}">${esc(STATUS[s] || s)}</span>`;

// ---------- уведомление справа внизу ----------

let toastTimer = null;
function toast(msg, ok) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = ok ? 'toast' : 'toast bad';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.hidden = true, 6000);
}
$('#toast').onclick = () => $('#toast').hidden = true;

// ---------- API ----------

async function api(method, path, body) {
  let r;
  try {
    r = await fetch('/admin' + path, {
      method,
      headers: { Authorization: 'Bearer ' + token, ...(body && { 'Content-Type': 'application/json' }) },
      body: body && JSON.stringify(body),
    });
  } catch { throw new Error('Нет связи с сервером. Попробуйте ещё раз.'); }
  if (r.status === 401) { logout('Пароль не подошёл. Введите заново.'); throw Object.assign(new Error('401'), { status: 401 }); }
  const data = await r.json().catch(() => null);
  if (!r.ok) throw Object.assign(new Error(errText(r.status, data)), { status: r.status });
  return data;
}

// STUB: формат тела 422 в api.md не описан. Разбираем `detail` как у FastAPI (список {loc, type})
// и по имени поля подбираем понятную фразу; если не вышло — общая фраза.
function errText(status, data) {
  if (status === 409) return 'Не удалось активировать: время голосования уже прошло.';
  if (status === 404) return 'Опрос не найден.';
  if (status !== 422) return 'Что-то пошло не так. Попробуйте ещё раз.';
  const msgs = (Array.isArray(data && data.detail) ? data.detail : []).map(e => {
    const field = (e.loc || []).at(-1);
    if (field === 'options') return e.type === 'too_long' ? 'Слишком много вариантов ответа.' : e.type === 'too_short' ? 'Нужно хотя бы два варианта ответа.' : 'Проверьте варианты ответа.';
    if (field === 'grace_s') return e.type === 'greater_than_equal' ? 'Задержка не может быть отрицательной.' : 'Задержка — это целое число секунд.';
    if (field === 'window_start') return 'Проверьте время начала.';
    if (field === 'window_end') return 'Проверьте время конца.';
    if (field === 'body' && e.type === 'value_error') return 'Конец должен быть позже начала.';
    return 'Проверьте, всё ли заполнено верно.';
  });
  return [...new Set(msgs)].join('\n') || 'Проверьте, всё ли заполнено верно.';
}

// ---------- вход ----------

function login(msg) {
  document.body.classList.add('anon');
  main.innerHTML = `<section class="narrow">
    <h1>Вход</h1>
    <p class="muted">${esc(msg || 'Пароль администратора.')}</p>
    <form id="lf"><input id="tok" type="password" autocomplete="off" placeholder="пароль"><button>Войти</button></form>
  </section>`;
  $('#tok').focus();
  $('#lf').onsubmit = e => {
    e.preventDefault();
    token = $('#tok').value.trim();
    if (!token) return toast('Введите пароль.');
    localStorage.setItem(KEY, token);
    route();
  };
}

function logout(msg) {
  gen++; clearTimeout(timer);
  token = '';
  localStorage.removeItem(KEY);
  login(msg);
}
$('#out').onclick = () => logout();

// ---------- маршруты: #/  #/new  #/p/{id} ----------

function route() {
  const g = ++gen;
  clearTimeout(timer);
  if (!token) return login();
  document.body.classList.remove('anon');
  const m = location.hash.match(/^#\/p\/([^/]+)$/);
  const view = m ? pollView(m[1], g) : location.hash === '#/new' ? newView() : listView(g);
  Promise.resolve(view).catch(e => {
    if (e.status !== 401 && g === gen) { main.innerHTML = '<p class="muted">Не удалось загрузить страницу.</p>'; toast(e.message); }
  });
}
window.addEventListener('hashchange', route);

// ---------- список ----------

async function listView(g) {
  main.innerHTML = '<p class="muted mono">загрузка…</p>';
  const polls = await api('GET', '/polls');
  if (g !== gen) return;
  main.innerHTML = `<div class="head"><h1>Опросы</h1><a class="btn" href="#/new">Новый опрос</a></div>` + (polls.length
    ? `<table class="list"><thead><tr><th>Вопрос</th><th>Статус</th><th>Начало</th></tr></thead><tbody>
      ${polls.map(p => `<tr data-id="${esc(p.id)}">
        <td><a href="#/p/${esc(p.id)}">${esc(p.question) || '<span class="muted">без вопроса</span>'}</a></td>
        <td>${badge(p.status)}</td>
        <td class="mono small muted">${when(p.window_start)}</td></tr>`).join('')}
      </tbody></table>`
    : '<p class="muted">Опросов пока нет.</p>');
  main.querySelectorAll('tr[data-id]').forEach(tr => tr.onclick = () => location.hash = '#/p/' + tr.dataset.id);
}

// ---------- создание ----------

function newView() {
  const start = new Date(Math.ceil(Date.now() / 60000) * 60000 + 10 * 60000);
  main.innerHTML = `<div class="head"><h1>Новый опрос</h1></div>
  <form id="nf" class="form" novalidate>
    <label><span>Вопрос</span><textarea name="question" rows="2"></textarea></label>
    <fieldset><legend>Как отвечают зрители</legend><div class="seg">
      <label><input type="radio" name="type" value="single" checked><b>Один вариант ответа</b><small>Зритель выбирает что-то одно</small></label>
      <label><input type="radio" name="type" value="multi"><b>Несколько вариантов ответа</b><small>Можно отметить сразу несколько</small></label>
    </div></fieldset>
    <fieldset><legend>Варианты ответа</legend><div id="opts"></div>
      <button type="button" id="add" class="ghost">Добавить вариант</button></fieldset>
    <fieldset><div class="row3">
      <label><span>Начало</span><input type="datetime-local" step="1" name="ws" value="${local(start)}"></label>
      <label><span>Конец</span><input type="datetime-local" step="1" name="we" value="${local(new Date(+start + 60000))}"></label>
      <label><span>Задержка, секунд</span><input type="number" name="grace" value="60"></label>
    </div>
    <p class="muted small hint">Задержка — сколько секунд после конца ещё принимать голоса: у части зрителей трансляция отстаёт.</p></fieldset>
    <div class="actions">
      <button>Создать</button>
      <button type="button" id="quick" class="ghost">Начать сейчас на 1 минуту</button>
    </div>
  </form>`;
  const form = $('#nf'), opts = $('#opts');
  const sync = () => [...opts.children].forEach((r, i) => r.firstChild.textContent = i + 1);
  const add = () => {
    const d = document.createElement('div');
    d.className = 'opt';
    d.innerHTML = '<span class="mono muted small"></span><input placeholder="вариант ответа"><button type="button" class="ghost">Убрать</button>';
    d.lastChild.onclick = () => { d.remove(); sync(); };
    opts.append(d); sync();
    return d;
  };
  add(); add();
  $('#add').onclick = () => add().children[1].focus();

  // Пустые поля ловим сами: сервер пустой вопрос и пустой вариант принимает, а пустое время в дату не превратить.
  const missing = f => !f.question.value.trim() ? 'Напишите вопрос.'
    : [...opts.querySelectorAll('input')].some(i => !i.value.trim()) ? 'Заполните все варианты ответа или уберите пустые.'
    : !f.ws.value ? 'Укажите время начала.'
    : !f.we.value ? 'Укажите время конца.'
    : f.grace.value === '' ? 'Укажите задержку в секундах.' : '';

  const create = async quick => {
    const f = form.elements;
    if (quick) { f.ws.value = local(new Date()); f.we.value = local(new Date(Date.now() + 60000)); }
    const miss = missing(f);
    if (miss) return toast(miss);
    const body = {
      question: f.question.value.trim(),
      type: f.type.value,
      options: [...opts.querySelectorAll('input')].map(i => i.value.trim()),
      window_start: new Date(f.ws.value).toISOString(),
      window_end: new Date(f.we.value).toISOString(),
      grace_s: Number(f.grace.value),
    };
    form.querySelectorAll('.actions button').forEach(b => b.disabled = true);
    try {
      const p = await api('POST', '/polls', body);
      // «Начать сейчас» сразу активирует: иначе минута уйдёт, пока админ дойдёт до кнопки.
      if (quick) await api('POST', `/polls/${p.id}/activate`);
      location.hash = '#/p/' + p.id;
    } catch (e) {
      if (e.status !== 401) toast(e.message);
      form.querySelectorAll('.actions button').forEach(b => b.disabled = false);
    }
  };
  form.onsubmit = e => { e.preventDefault(); create(false); };
  $('#quick').onclick = () => create(true);
}

// ---------- карточка ----------

const optList = p => `<h2>Варианты ответа</h2>
  <ol class="optlist">${p.options.map(o => `<li><span class="mono">${o.idx + 1}</span>${esc(o.label)}</li>`).join('')}</ol>`;

async function pollView(id, g) {
  const p = await api('GET', '/polls/' + id);
  if (g !== gen) return;
  const url = `${location.origin}/p/${p.id}`;
  main.innerHTML = `<a class="mono small muted" href="#/">← все опросы</a>
  <div class="head" style="margin-top:32px"><h1>${esc(p.question)}</h1><span id="st">${badge(p.status)}</span></div>
  <section id="an">${p.status === 'draft' ? `<div class="activate"><button id="act">Активировать</button>
    <p class="muted small">Пока опрос в черновике, зрители его не видят.</p></div>${optList(p)}` : '<p class="muted mono">загрузка…</p>'}</section>
  <h2>Ссылка для зрителей</h2>
  <div class="share"><div class="qr" id="qr"></div>
    <div><code>${esc(url)}</code>
      <a class="btn ghost" href="${esc(url)}" target="_blank">Открыть</a>
      <button class="ghost" id="copy">Скопировать</button>
      <p class="muted small">Покажите QR-код в эфире — зрители наведут камеру телефона и попадут на страницу голосования.</p></div></div>
  <p class="tech">${TYPE[p.type] || esc(p.type)} · начало ${dt(p.window_start)} · конец ${dt(p.window_end)} · задержка ${p.grace_s} с · номер ${esc(p.id)}</p>`;
  // QR ссылки: qrcode-generator (vendor/qrcode.js), тип подбирается сам, коррекция M, тихая зона 4 модуля.
  const qr = qrcode(0, 'M');
  qr.addData(url);
  qr.make();
  $('#qr').innerHTML = qr.createSvgTag({ cellSize: 1, margin: 4, scalable: true, title: url });
  $('#copy').onclick = () => navigator.clipboard.writeText(url).then(() => toast('Ссылка скопирована.', true), () => toast('Не удалось скопировать ссылку.'));
  if ($('#act')) $('#act').onclick = async () => {
    $('#act').disabled = true;
    try { await api('POST', `/polls/${p.id}/activate`); route(); }
    catch (e) { if (e.status !== 401) toast(e.message); $('#act').disabled = false; }
  };
  // В draft голосов нет, а что отдаёт аналитика в draft, api.md не говорит — не спрашиваем.
  if (p.status !== 'draft') loop(p, g);
}

// ---------- аналитика: раз в секунду, пока не final ----------

async function loop(p, g) {
  let final = false;
  try {
    const [res, an] = await Promise.all([api('GET', `/polls/${p.id}/results`), api('GET', `/polls/${p.id}/analytics`)]);
    if (g !== gen) return;
    final = res.status === 'final';
    $('#st').innerHTML = badge(res.status);
    $('#an').innerHTML = final ? finalHtml(res, an, p) : liveHtml(res, an, p);
  } catch (e) {
    if (e.status === 401 || g !== gen) return;
    toast('Не удалось обновить данные. Пробуем ещё раз.');
  }
  if (!final && g === gen) timer = setTimeout(() => loop(p, g), 1000);
}

function liveHtml(res, an, p) {
  return `<div class="kpi"><div class="big">${num(res.received)}</div>
    <div class="muted">${res.status === 'counting' ? 'голосов получено · голосование закрыто, считаем итоги' : 'голосов получено'}</div></div>
  <p class="muted note">Итоги по вариантам появятся после подсчёта. Пока идёт голосование, их не показываем, чтобы не подталкивать зрителей к лидеру.</p>
  <h2>Голоса по секундам</h2>${chart(an, p)}
  ${optList(p)}`;
}

function finalHtml(res, an, p) {
  const f = an.funnel, rj = f && f.rejected, ipc = an.ip_concentration || [];
  const bar = (v, base) => `<div class="track"><div class="fill" style="width:${base ? v / base * 100 : 0}%"></div></div>`;
  // воронка: полосы вплотную, по центру, сужаются сверху вниз
  const step = (label, v) => `<div>${label}</div><div class="cone"><div class="fill" style="width:${v / f.received * 100}%"></div></div><div class="val">${num(v)}</div>`;
  const why = (label, v, hint) => `<div>${label}<span class="hint">${hint}</span></div><div class="val">${num(v)}</div>`;
  const ipVotes = ipc.reduce((s, b) => s + b.votes, 0);
  const bucket = b => b.replace('-', '–').replace('+', ' и больше');
  return `<div class="kpis">
    <div class="kpi"><div class="big">${num(res.counted)}</div><div class="muted">голосов засчитано</div></div>
    <div class="kpi"><div class="mid">${num(res.received)}</div><div class="muted">получено всего</div></div>
    <div class="kpi"><div class="mid">${pct(res.over_limit_share)}</div><div class="muted">отсеяно как подозрительные</div></div>
  </div>
  <h2>Итоги</h2>
  ${p.type === 'multi' ? '<p class="muted small note top">Зрители могли выбрать несколько вариантов, поэтому в сумме больше 100%.</p>' : ''}
  <div class="bars">${res.options.map(o => `<div>
    <div class="bar-top"><span>${esc(o.label)}</span><span class="mono">${pct(o.share)}</span></div>
    ${bar(o.share || 0, 1)}
    <div class="mono muted small">${num(o.counted)} засчитано из ${num(o.total)}</div></div>`).join('')}</div>
  <h2>Голоса по секундам</h2>${chart(an, p)}
  ${f && f.received ? `<h2>Какие голоса засчитаны</h2><div class="funnel">
    ${step('Всего голосов', f.received)}
    ${step('Разные зрители', f.unique_voters)}
    ${step('Засчитано', f.counted)}
  </div>
  <div class="why">
    ${why('Повторные голоса из того же браузера', rj.repeat_voter, 'Засчитываем только первый голос.')}
    ${why('Похожие устройства сверх лимита', rj.key_limit, 'С одного адреса голосовало слишком много одинаковых устройств.')}
    ${why('Слишком много голосов с одного адреса', rj.ip_ceiling, 'Больше 5000 голосов в минуту.')}
  </div>` : ''}
  ${ipc.length ? `<h2>Сколько голосов пришло с одного адреса</h2>
  <table class="ipc"><thead><tr><th>Голосов с одного адреса</th><th class="r">Адресов</th><th class="r">Голосов</th><th>Доля голосов</th></tr></thead><tbody>
    ${ipc.map(b => `<tr><td class="mono">${esc(bucket(b.bucket))}</td><td class="mono r">${num(b.ips)}</td><td class="mono r">${num(b.votes)}</td>
      <td><span class="mono small">${pct(ipVotes ? b.votes / ipVotes : null)}</span>${bar(b.votes, ipVotes)}</td></tr>`).join('')}
  </tbody></table>
  <p class="muted small">Повторные голоса из того же браузера здесь не учтены.</p>` : ''}`;
}

// ---------- график голосов по секундам + ожидаемая кривая (логнормальная модель) ----------

// erf по Абрамовицу–Стигану 7.1.26, погрешность < 1.5e-7
function erf(x) {
  const s = Math.sign(x), a = Math.abs(x), t = 1 / (1 + 0.3275911 * a);
  return s * (1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-a * a));
}
const cdf = (t, m) => t <= 0 ? 0 : 0.5 * (1 + erf(Math.log(t / m.median_s) / (m.sigma * Math.SQRT2)));
const nice = v => { const p = 10 ** Math.floor(Math.log10(v)); const f = v / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; };

function chart(an, p) {
  const tl = an.timeline || [], m = an.model;
  const W = Math.max(320, main.clientWidth - 64), H = 260, L = 64, R = 24, T = 16, B = 32;
  const win = (new Date(p.window_end) - new Date(p.window_start)) / 1000;
  const n = Math.max(tl.length, Math.ceil(win + p.grace_s), 1);
  const total = tl.reduce((s, x) => s + x.received, 0);
  const model = m ? Array.from({ length: n }, (_, t) => total * (cdf(t + 1, m) - cdf(t, m))) : [];
  const max = Math.max(1, ...tl.map(x => x.received), ...model);
  const ys = nice(max / 4), top = Math.ceil(max / ys) * ys, xs = nice(n / Math.max(3, W / 110));
  const bw = (W - L - R) / n;
  const x = t => L + t * bw, y = v => T + (1 - v / top) * (H - T - B);
  let s = '';
  for (let v = 0; v <= top; v += ys) s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${short(v)}</text>`;
  for (let t = 0; t <= n; t += xs) s += `<text x="${x(t)}" y="${H - 10}" text-anchor="middle">${t} с</text>`;
  const gap = bw > 4 ? 1 : 0;
  for (const d of tl) {
    const h = y(0) - y(d.received);
    s += `<g><rect class="bar" x="${x(d.t) + gap}" y="${y(d.received)}" width="${bw - 2 * gap}" height="${h}"/>`
      + `<line class="top" x1="${x(d.t) + gap}" x2="${x(d.t + 1) - gap}" y1="${y(d.received)}" y2="${y(d.received)}"/>`
      + `<rect class="hit" x="${x(d.t)}" y="${T}" width="${bw}" height="${H - T - B}"><title>${d.t}–${d.t + 1} с: ${num(d.received)} голосов${m ? ', ожидали ' + num(Math.round(model[d.t] || 0)) : ''}</title></rect></g>`;
  }
  if (win < n) s += `<line class="end" x1="${x(win)}" x2="${x(win)}" y1="${T}" y2="${y(0)}"/><text x="${x(win) + 6}" y="${T + 10}">конец</text>`;
  if (m) s += `<polyline class="model" points="${model.map((v, t) => `${x(t + .5).toFixed(1)},${y(v).toFixed(1)}`).join(' ')}"/>`;
  return `<div class="legend"><span><i class="key"></i>голосов за секунду</span>
    ${m ? '<span><i class="key line"></i>ожидали</span>' : ''}</div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Голоса по секундам">${s}</svg>`;
}

route();
