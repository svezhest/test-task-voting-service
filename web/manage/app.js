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
const STATUS = { draft: 'Черновик', planned: 'Запланирован', active: 'Идёт голосование', counting: 'Подсчёт', final: 'Готово' };
const TYPE = { single: 'Один вариант ответа', multi: 'Несколько вариантов ответа' };
// Показываемый статус считаем из status и часов: active до начала окна — «Запланирован»,
// после window_end + grace_s, пока сервер не перевёл в counting, — уже «Подсчёт».
const phase = (p, status = p.status) => {
  if (status !== 'active') return status;
  const now = Date.now();
  return now < new Date(p.window_start) ? 'planned' : now <= +new Date(p.window_end) + p.grace_s * 1000 ? 'active' : 'counting';
};
const badge = (p, status) => { const s = phase(p, status); return `<span class="badge s-${s}">${STATUS[s]}</span>`; };

// ---------- местное время для полей datetime-local ----------

const pad = n => String(n).padStart(2, '0');
// Date -> «2026-10-01T21:00:00» по часам компьютера
const local = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const offset = d => { const o = -d.getTimezoneOffset(); return (o < 0 ? '-' : '+') + pad(Math.floor(Math.abs(o) / 60)) + ':' + pad(Math.abs(o) % 60); };
// ISO 8601 со смещением компьютера: «2026-10-01T21:00:00+03:00»
const iso = d => local(d) + offset(d);
const zone = d => { const o = -d.getTimezoneOffset(), h = Math.floor(Math.abs(o) / 60), m = Math.abs(o) % 60; return 'UTC' + (o < 0 ? '−' : '+') + h + (m ? ':' + pad(m) : ''); };

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
  if (r.status === 401) { logout('Токен не подошёл. Введите заново.'); throw Object.assign(new Error('401'), { status: 401 }); }
  const data = await r.json().catch(() => null);
  if (!r.ok) throw Object.assign(new Error(errText(r.status, data)), { status: r.status });
  return data;
}

// 422 у POST и PATCH — список `detail`, как у FastAPI: {loc, type, msg}. Фразу подбираем по полю из loc
// (у POST loc начинается с 'body', у PATCH — нет).
// 409 здесь не бывает: его разбирает каждая кнопка сама.
const EMPTY_OPT = 'Заполните все варианты ответа или уберите пустые.';
function errText(status, data) {
  if (status === 404) return 'Опрос не найден.';
  if (status !== 422) return 'Что-то пошло не так. Попробуйте ещё раз.';
  const msgs = data.detail.map(({ loc, type }) => {
    const [field, idx] = loc.filter(x => x !== 'body');
    if (field === 'question') return 'Напишите вопрос.';
    if (field === 'options') return idx != null ? EMPTY_OPT
      : type === 'too_long' ? 'Слишком много вариантов ответа.'
      : type === 'too_short' ? 'Нужно хотя бы два варианта ответа.' : EMPTY_OPT;
    if (field === 'grace_s') return type === 'greater_than_equal' ? 'Задержка не может быть отрицательной.' : 'Задержка — это целое число секунд.';
    if (field === 'window_start') return 'Проверьте время начала.';
    if (field === 'window_end') return 'Проверьте время конца.';
    // loc без поля — правило на весь опрос; оно одно: конец позже начала
    if (field == null) return 'Конец должен быть позже начала.';
    return 'Проверьте, всё ли заполнено верно.';
  });
  return [...new Set(msgs)].join('\n');
}

// ---------- вход ----------

function login(msg) {
  document.body.classList.add('anon');
  main.innerHTML = `<section class="narrow">
    <h1>Вход</h1>
    <p class="muted">${esc(msg || 'Токен администратора.')}</p>
    <form id="lf"><input id="tok" type="password" autocomplete="off" placeholder="токен"><button>Войти</button></form>
  </section>`;
  $('#tok').focus();
  $('#lf').onsubmit = e => {
    e.preventDefault();
    const t = $('#tok').value.trim();
    if (!t) return toast('Введите токен.');
    // токен уходит в заголовок: не-ASCII (русская раскладка) fetch не отправит
    if (!/^[\x20-\x7e]+$/.test(t)) return toast('Токен не подошёл: проверьте раскладку клавиатуры.');
    localStorage.setItem(KEY, token = t);
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

// ---------- флажок «из интернета»: Cloudflare-туннель на страницу голосования ----------

const netBox = $('#net');
const syncNet = () => api('GET', '/public-url').then(pub => netBox.checked = !!pub.tunnel, () => {});
// Открыть или закрыть туннель. Открытие ждёт, пока туннель поднимется (до 30 с).
async function setNet(on) {
  netBox.disabled = true;
  try {
    if (on) {
      toast('Открываем страницу голосования из интернета…', true);
      await api('POST', '/tunnel');
      toast('Страница голосования открыта из интернета.', true);
    } else {
      await api('DELETE', '/tunnel');
      toast('Доступ из интернета закрыт. Голосовать можно только в локальной сети.', true);
    }
  } catch (e) {
    if (e.status !== 401) toast(e.status === 503 ? 'Не удалось открыть доступ из интернета: туннель не поднялся. Попробуйте ещё раз.' : e.message);
  }
  netBox.disabled = false;
  await syncNet();
}
// Карточку опроса перерисовываем: ссылка и QR зависят от туннеля. Форму не трогаем, чтобы не потерять введённое.
netBox.onchange = async () => {
  await setNet(netBox.checked);
  if (/^#\/p\/[^/]+$/.test(location.hash)) route();
};

// ---------- маршруты: #/  #/new  #/p/{id}  #/p/{id}/edit ----------

function route() {
  const g = ++gen;
  clearTimeout(timer);
  if (!token) return login();
  document.body.classList.remove('anon');
  syncNet();
  const m = location.hash.match(/^#\/p\/([^/]+)(\/edit)?$/);
  const view = m ? (m[2] ? editView(m[1], g) : pollView(m[1], g)) : location.hash === '#/new' ? formView() : listView(g);
  Promise.resolve(view).catch(e => {
    if (e.status !== 401 && g === gen) { main.innerHTML = '<p class="muted">Не удалось загрузить страницу.</p>'; toast(e.message); }
  });
}
window.addEventListener('hashchange', route);

// ---------- список ----------

const plural = (n, one, few, many) => n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;
const polls_ = n => `${num(n)} ${plural(n, 'опрос', 'опроса', 'опросов')}`;
const dayKey = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;   // местная дата «2026-09-30»
const startKey = p => dayKey(new Date(p.window_start));
const MON = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const dayName = k => { const [y, m, d] = k.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) + ' ' + y; };
const monName = k => { const [y, m] = k.split('-').map(Number); return new Date(y, m - 1).toLocaleDateString('ru-RU', { month: 'long' }) + ' ' + y; };
// подсветка совпадений: текст экранируем по кусочкам
const hl = (s, q) => {
  if (!q) return esc(s);
  let out = '', i = 0, j;
  while ((j = s.toLowerCase().indexOf(q, i)) >= 0) { out += esc(s.slice(i, j)) + '<mark>' + esc(s.slice(j, j + q.length)) + '</mark>'; i = j + q.length; }
  return out + esc(s.slice(i));
};

let pick = '', query = '', page = 1;   // фильтр списка: день «2026-09-30» или месяц «2026-09»; текст поиска; страница — живут, пока открыта вкладка
const PER = 20;   // опросов на странице

async function listView(g) {
  main.innerHTML = '<p class="muted mono">загрузка…</p>';
  let polls = await api('GET', '/polls');
  if (g !== gen) return;
  main.innerHTML = `<div class="head"><h1>Опросы</h1><a class="btn" href="#/new">Новый опрос</a></div>
    ${calendar(polls)}
    ${polls.length ? `<div class="filter"><p id="sum" class="mono"></p><button id="reset">Сбросить фильтры</button></div>
    <input id="q" placeholder="Поиск по вопросам и вариантам" autocomplete="off" enterkeyhint="search">
    <div id="rows"></div>` : '<p class="muted">Опросов пока нет.</p>'}`;
  const sc = $('.cal-scroll');
  sc.scrollLeft = sc.scrollWidth;   // на узком экране — сразу к последним неделям
  // подсказка над днём: дата и число опросов
  const tip = $('#tip'), inner = $('.cal-in');
  inner.onmouseover = e => {
    const c = e.target.dataset.d && e.target;
    tip.hidden = !c;
    if (!c) return;
    tip.textContent = c.dataset.t;
    const x = c.offsetLeft + c.offsetWidth / 2, w = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(x, w / 2), inner.offsetWidth - w / 2) + 'px';
    tip.style.top = c.offsetTop + 'px';
  };
  inner.onmouseleave = () => tip.hidden = true;
  if (!polls.length) return;
  // повторный клик по тому же дню или месяцу — сброс
  inner.onclick = e => { const k = e.target.closest('[data-d], [data-m]'); if (k) { const v = k.dataset.d || k.dataset.m; pick = pick === v ? '' : v; page = 1; apply(polls); } };
  $('#q').value = query;
  $('#q').oninput = () => { query = $('#q').value; page = 1; apply(polls); };
  $('#reset').onclick = () => { pick = query = ''; page = 1; $('#q').value = ''; apply(polls); };
  apply(polls);
  // Раз в секунду: статусы в строках, мигание в календаре и счётчик «идут сейчас» — по часам.
  // Раз в 10 с перечитываем список: так видно «Готово» и досрочное завершение. Фильтр, поиск и страницу не трогаем.
  let sec = 0;
  const tick = async () => {
    if (++sec % 10 === 0) {
      const fresh = await api('GET', '/polls').catch(() => null);
      if (g !== gen) return;
      if (fresh) apply(polls = fresh);
    }
    const act = polls.filter(p => phase(p) === 'active'), live = {};
    for (const p of act) { const k = startKey(p); live[k] = live[k.slice(0, 7)] = true; }
    main.querySelectorAll('.cal [data-d]').forEach(c => c.classList.toggle('live', !!live[c.dataset.d]));
    main.querySelectorAll('.mon').forEach(b => b.firstChild.classList.toggle('live', !!live[b.dataset.m]));
    $('#now').innerHTML = act.length ? ` · <span class="go">${num(act.length)} ${plural(act.length, 'идёт', 'идут', 'идут')} сейчас</span>` : '';
    const byId = Object.fromEntries(polls.map(p => [p.id, p]));
    main.querySelectorAll('tr[data-id]').forEach(tr => { const b = badge(byId[tr.dataset.id]); if (tr.children[1].innerHTML !== b) tr.children[1].innerHTML = b; });
    timer = setTimeout(tick, 1000);
  };
  tick();
}

// Календарь как у GitHub: колонки — недели с понедельника, строки — дни; последние 12 месяцев до конца текущей недели.
function calendar(polls) {
  const n = {};   // мигание «идёт голосование» расставляет tick()
  for (const p of polls) {
    const k = startKey(p), m = k.slice(0, 7);
    n[k] = (n[k] || 0) + 1; n[m] = (n[m] || 0) + 1;
  }
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const d = new Date(today); d.setDate(d.getDate() - 364); d.setDate(d.getDate() - (d.getDay() + 6) % 7);
  const end = new Date(today); end.setDate(end.getDate() + 6 - (today.getDay() + 6) % 7);
  const keys = Object.keys(n), max = k => Math.max(1, ...keys.filter(x => x.length === k).map(x => n[x]));
  const dmax = max(10), mmax = max(7);
  // насыщенность 0–4 по логарифму: один опрос в день заметен и рядом с днём, где их сотня
  const lvl = (c, top) => c ? Math.ceil(4 * Math.log1p(c) / Math.log1p(top)) : 0;
  let days = '', year = 0;
  const heads = [];
  while (d <= end) {
    for (let i = 0; i < 7; i++, d.setDate(d.getDate() + 1)) {
      const k = dayKey(d), c = n[k] || 0;
      if (d <= today) year += c;
      days += `<i data-d="${k}" data-t="${dayName(k)} · ${c ? polls_(c) : 'опросов нет'}" class="l${lvl(c, dmax)}${d > today && !c ? ' later' : ''}"></i>`;
    }
    // неделя относится к месяцу своего воскресенья: подпись месяца встаёт над неделей с его первым числом;
    // у месяца в одну неделю подписи нет — кроме текущего, его подпись заходит за край
    const m = dayKey(new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1)).slice(0, 7);
    if (heads.length && heads.at(-1).m === m) heads.at(-1).span++; else heads.push({ m, span: 1 });
  }
  return `<section class="cal">
    <div class="cal-top"><p class="mono"><b>${polls_(year)}</b> за год<span id="now"></span></p>
      <p class="scale mono small muted">меньше${[0, 1, 2, 3, 4].map(l => `<i class="l${l}"></i>`).join('')}больше</p></div>
    <div class="cal-scroll"><div class="cal-in">
      <div class="months"><span></span>${heads.map((h, i) => h.span < 2 && i < heads.length - 1 ? `<span style="grid-column:span ${h.span}"></span>`
        : `<button class="mon" data-m="${h.m}" style="grid-column:span ${h.span}" title="${monName(h.m)} · ${n[h.m] ? polls_(n[h.m]) : 'опросов нет'}"><i class="l${lvl(n[h.m] || 0, mmax)}"></i>${MON[+h.m.slice(5) - 1]}</button>`).join('')}</div>
      <div class="days"><span></span><span>пн</span><span></span><span>ср</span><span></span><span>пт</span><span></span>${days}</div>
      <div id="tip" class="tip mono" hidden></div>
    </div></div></section>`;
}

function apply(polls) {
  const q = query.trim().toLowerCase(), has = s => s.toLowerCase().includes(q);
  const shown = polls.filter(p => startKey(p).startsWith(pick) && (!q || has(p.question) || p.options.some(o => has(o.label))));
  main.querySelectorAll('.cal [data-d]').forEach(c => { c.classList.toggle('dim', !!pick && !c.dataset.d.startsWith(pick)); c.classList.toggle('sel', c.dataset.d === pick); });
  main.querySelectorAll('.mon').forEach(b => b.classList.toggle('sel', b.dataset.m === pick));
  const where = (pick ? (pick.length > 7 ? 'За ' + dayName(pick) : 'За ' + monName(pick)) : '') + (q ? (pick ? ' по' : 'По') + ` запросу «${esc(query.trim())}»` : '');
  $('#sum').innerHTML = where ? `${where}: <b>${polls_(shown.length)}</b>` : `Все опросы: <b>${num(polls.length)}</b>`;
  $('#reset').hidden = !where;
  // страница: если после обновления её не стало — последняя
  const pages = Math.max(1, Math.ceil(shown.length / PER));
  page = Math.min(page, pages);
  const from = (page - 1) * PER;
  $('#rows').innerHTML = shown.length ? `<table class="list"><thead><tr><th>Вопрос</th><th>Статус</th><th>Начало</th></tr></thead><tbody>
    ${shown.slice(from, from + PER).map(p => {
      // при поиске под вопросом — только подходящие варианты, иначе все
      const opts = q && p.options.some(o => has(o.label)) ? p.options.filter(o => has(o.label)) : p.options;
      return `<tr data-id="${esc(p.id)}">
      <td><a href="#/p/${esc(p.id)}">${hl(p.question, q)}</a>
        <div class="opts">${opts.map(o => hl(o.label, q)).join('<span> · </span>')}</div></td>
      <td>${badge(p)}</td>
      <td class="mono small muted">${when(p.window_start)}</td></tr>`;
    }).join('')}</tbody></table>${pages > 1 ? pager(pages, from, shown.length) : ''}`
    : '<div class="empty"><p>Ничего не нашлось.</p><p class="muted small">Попробуйте другое слово или сбросьте фильтры.</p></div>';
  main.querySelectorAll('tr[data-id]').forEach(tr => tr.onclick = () => location.hash = '#/p/' + tr.dataset.id);
  // к новой странице — с её начала, если начало списка ушло за верх экрана
  main.querySelectorAll('.pages [data-p]').forEach(b => b.onclick = () => {
    page = +b.dataset.p; apply(polls);
    if ($('#rows').getBoundingClientRect().top < 0) $('#rows').scrollIntoView();
  });
}

// Пейджер: всегда семь мест под номера, чтобы стрелки не прыгали — 1 2 3 4 5 … 12, 1 … 4 5 6 … 12, 1 … 8 9 10 11 12.
function pager(n, from, total) {
  const nums = n <= 7 ? Array.from({ length: n }, (_, i) => i + 1)
    : page <= 4 ? [1, 2, 3, 4, 5, 0, n]
    : page >= n - 3 ? [1, 0, n - 4, n - 3, n - 2, n - 1, n]
    : [1, 0, page - 1, page, page + 1, 0, n];
  const btn = (p, text, label) => `<button class="ghost" data-p="${p}" aria-label="${label}"${p < 1 || p > n ? ' disabled' : ''}>${text}</button>`;
  return `<div class="pager" role="navigation" aria-label="Страницы списка">
    <div class="pages">${btn(page - 1, '←', 'Предыдущая страница')}${nums.map(i => !i ? '<span>…</span>'
      : i === page ? `<button class="on" aria-current="page">${i}</button>` : btn(i, i, 'Страница ' + i)).join('')}${btn(page + 1, '→', 'Следующая страница')}</div>
    <p class="mono muted">${num(from + 1)}–${num(Math.min(from + PER, total))} из ${num(total)}</p></div>`;
}

// ---------- создание и правка черновика: одна форма ----------

async function editView(id, g) {
  const p = await api('GET', '/polls/' + id);
  if (g !== gen) return;
  if (p.status !== 'draft') { toast('Опрос уже запущен, изменить его нельзя.'); location.hash = '#/p/' + id; return; }
  formView(p);
}

function formView(p) {
  // по умолчанию: начало через 5 минут по часам компьютера, до целой минуты вверх; конец — через минуту после начала
  const start = p ? new Date(p.window_start) : new Date(Math.ceil((Date.now() + 5 * 60000) / 60000) * 60000);
  const end = p ? new Date(p.window_end) : new Date(+start + 60000);
  main.innerHTML = `${p ? `<a class="mono small muted" href="#/p/${esc(p.id)}">← к опросу</a>` : ''}
  <div class="head"${p ? ' style="margin-top:32px"' : ''}><h1>${p ? 'Изменить опрос' : 'Новый опрос'}</h1></div>
  <form id="nf" class="form" novalidate>
    <label><span>Вопрос</span><textarea name="question" rows="2"></textarea></label>
    <fieldset><legend>Как отвечают зрители</legend><div class="seg">
      <label><input type="radio" name="type" value="single"><b>Один вариант ответа</b><small>Зритель выбирает что-то одно</small></label>
      <label><input type="radio" name="type" value="multi"><b>Несколько вариантов ответа</b><small>Можно отметить сразу несколько</small></label>
    </div></fieldset>
    <fieldset><legend>Варианты ответа</legend><div id="opts"></div>
      <button type="button" id="add" class="ghost">Добавить вариант</button></fieldset>
    <fieldset><div class="row3">
      <label><span>Начало</span><input type="datetime-local" step="1" name="ws" value="${local(start)}"></label>
      <label><span>Конец</span><input type="datetime-local" step="1" name="we" value="${local(end)}"></label>
      <label><span>Задержка, секунд</span><input type="number" name="grace" value="${p ? p.grace_s : 60}"></label>
    </div>
    <p class="muted small hint">Время местное, по часам этого компьютера (${zone(start)}).
      Задержка — сколько секунд после конца ещё принимать голоса: у части зрителей трансляция отстаёт.</p>
    ${start.getTimezoneOffset() === 0 ? '<p class="muted tz">Браузер показывает время по UTC. Если вы не в этом поясе, браузер его скрывает (защита от слежки, как в LibreWolf или Tor): вводите время по UTC</p>' : ''}</fieldset>
    <div class="actions">
      <button>${p ? 'Сохранить' : 'Создать черновик'}</button>
      <a class="btn ghost" href="${p ? '#/p/' + esc(p.id) : '#/'}">Отмена</a>
    </div>
  </form>`;
  const form = $('#nf'), opts = $('#opts');
  form.elements.question.value = p ? p.question : '';
  form.elements.type.value = p ? p.type : 'single';
  const sync = () => [...opts.children].forEach((r, i) => r.firstChild.textContent = i + 1);
  const add = label => {
    const d = document.createElement('div');
    d.className = 'opt';
    d.innerHTML = '<span class="mono muted small"></span><input placeholder="вариант ответа"><button type="button" class="ghost">Убрать</button>';
    d.children[1].value = label || '';
    d.lastChild.onclick = () => { d.remove(); sync(); };
    opts.append(d); sync();
    return d;
  };
  (p ? p.options.map(o => o.label) : ['', '']).forEach(l => add(l));
  $('#add').onclick = () => add().children[1].focus();

  // Пустые поля ловим сами, до сервера: так фраза точнее, а пустое время в дату не превратить.
  const missing = f => !f.question.value.trim() ? 'Напишите вопрос.'
    : [...opts.querySelectorAll('input')].some(i => !i.value.trim()) ? EMPTY_OPT
    : !f.ws.value ? 'Укажите время начала.'
    : !f.we.value ? 'Укажите время конца.'
    : f.grace.value === '' ? 'Укажите задержку в секундах.' : '';

  const buttons = on => form.querySelectorAll('.actions button').forEach(b => b.disabled = !on);
  form.onsubmit = async e => {
    e.preventDefault();
    const f = form.elements;
    const miss = missing(f);
    if (miss) return toast(miss);
    const body = {
      question: f.question.value.trim(),
      type: f.type.value,
      options: [...opts.querySelectorAll('input')].map(i => i.value.trim()),
      window_start: iso(new Date(f.ws.value)),
      window_end: iso(new Date(f.we.value)),
      grace_s: Number(f.grace.value),
    };
    buttons(false);
    let saved;
    try {
      saved = p ? await api('PATCH', '/polls/' + p.id, body) : await api('POST', '/polls', body);
    } catch (e) {
      if (e.status === 409) { toast('Опрос уже запущен, изменить его нельзя.'); location.hash = '#/p/' + p.id; return; }
      if (e.status !== 401) toast(e.message);
      buttons(true);
      return;
    }
    toast(p ? 'Изменения сохранены.' : 'Черновик создан. Проверьте его и запустите.', true);
    location.hash = '#/p/' + saved.id;
  };
}

// ---------- своё квадратное окошко подтверждения ----------

// buttons: [[значение, надпись, класс]]; фокус на последней. Esc — пустое значение.
function dialog(title, texts, buttons) {
  return new Promise(resolve => {
    const d = document.createElement('dialog');
    d.innerHTML = `<p class="dlg-title">${esc(title)}</p>${texts.map(t => `<p>${esc(t)}</p>`).join('')}
      <form method="dialog" class="actions">${buttons.map(([v, label, cls = ''], i) =>
        `<button value="${v}" class="${cls}"${i === buttons.length - 1 ? ' autofocus' : ''}>${esc(label)}</button>`).join('')}</form>`;
    document.body.append(d);
    d.onclose = () => { d.remove(); resolve(d.returnValue); };
    // клик мимо окна — как Esc: закрыть, ничего не выбрав
    d.onclick = e => {
      const box = d.getBoundingClientRect();
      const outside = e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom;
      if (e.target === d && outside) d.close('');
    };
    d.showModal();
  });
}
const ask = async (title, text, yes, cls = '') => await dialog(title, [text], [['yes', yes, cls], ['', 'Отмена', 'ghost']]) === 'yes';
// Если, пока окно открыто, экран сменился (опрос завершился, ушли на другую страницу), действие не выполняем и говорим об этом.
const stale = g => g === gen || toast('Пока было открыто окно, страница обновилась. Проверьте опрос и повторите действие.');
const sure = async (g, ...a) => await ask(...a) && stale(g);

async function remove(p, btn, g) {
  if (!await sure(g, 'Удалить опрос?', `«${p.question}» удалится насовсем${p.status === 'final' ? ' вместе с итогами' : ''}. Вернуть его будет нельзя.`, 'Удалить', 'danger')) return;
  btn.disabled = true;
  try {
    await api('DELETE', '/polls/' + p.id);
    location.hash = '#/';
    toast('Опрос удалён.', true);
  } catch (e) {
    if (e.status === 409) { toast('Пока идёт голосование или подсчёт, удалить опрос нельзя.'); route(); return; }
    if (e.status !== 401) toast(e.message);
    btn.disabled = false;
  }
}

// ---------- карточка ----------

const optList = p => `<h2>Варианты ответа</h2>
  <ol class="optlist">${p.options.map(o => `<li><span class="mono">${o.idx + 1}</span>${esc(o.label)}</li>`).join('')}</ol>`;

// Ссылка и QR для зрителей — только пока опрос не завершён.
const shareHtml = pub => `<section class="share"><div class="qr" id="qr"></div>
  <div><span class="label">Ссылка для зрителей</span>
    ${pub.tunnel && pub.lan ? '<div class="via mono small"><button class="link" data-via="tunnel">через интернет</button><button class="link" data-via="lan">в локальной сети</button></div>' : ''}
    <code id="url"></code><p class="muted small" id="where"></p>
    <a class="btn ghost" id="open" target="_blank">Открыть</a>
    <button class="ghost" id="copy">Скопировать</button>
    <p class="muted small">Покажите QR-код в эфире — зрители наведут камеру телефона и попадут на страницу голосования.</p></div></section>`;

// Адрес для телефона: туннель, если есть, иначе адрес в локальной сети, иначе зрительский вход на этом компьютере.
function share(pub, via, id) {
  const url = `${pub[via] || 'http://localhost:8090'}/p/${id}`;
  $('#url').textContent = url;
  $('#open').href = url;
  $('#where').textContent = pub[via] ? (via === 'tunnel' ? 'Откроется с любого телефона.' : 'Телефон должен быть в той же Wi-Fi, что и этот компьютер.') : '';
  $('#where').hidden = !pub[via];
  document.querySelectorAll('[data-via]').forEach(b => { b.classList.toggle('on', b.dataset.via === via); b.onclick = () => share(pub, b.dataset.via, id); });
  // QR ссылки: qrcode-generator (vendor/qrcode.js), тип подбирается сам, коррекция M; тихую зону добирает светлая рамка.
  const qr = qrcode(0, 'M');
  qr.addData(url);
  qr.make();
  $('#qr').innerHTML = qr.createSvgTag({ cellSize: 1, margin: 2, scalable: true, title: url });
  // по http (адрес не localhost) navigator.clipboard нет: выделяем ссылку, копирует сам админ
  const select = () => { getSelection().selectAllChildren($('#url')); toast('Выделено — нажмите Ctrl+C / ⌘C', true); };
  $('#copy').onclick = () => navigator.clipboard ? navigator.clipboard.writeText(url).then(() => toast('Ссылка скопирована.', true), select) : select();
}

async function pollView(id, g) {
  const [p, pub] = await Promise.all([api('GET', '/polls/' + id), api('GET', '/public-url').catch(() => ({}))]);
  if (g !== gen) return;
  const draft = p.status === 'draft', final = p.status === 'final';
  main.innerHTML = `<a class="mono small muted" href="#/">← все опросы</a>
  <div class="head" style="margin-top:32px"><h1>${esc(p.question)}</h1><span id="st">${badge(p)}</span></div>
  ${draft || final ? '' : '<p class="notice" id="notice"></p><div class="actions" id="finrow"><button class="ghost" id="fin">Завершить досрочно</button></div>'}
  ${draft ? `<div class="actions"><button id="act">Запустить</button>
    <a class="btn ghost" href="#/p/${esc(p.id)}/edit">Изменить</a><button class="ghost" id="del">Удалить</button></div>` : ''}
  ${final ? '' : shareHtml(pub)}
  <section id="an">${draft ? optList(p) : '<p class="muted mono">загрузка…</p>'}</section>
  ${final ? '<div class="del-row"><button class="ghost" id="del">Удалить опрос</button></div>' : ''}
  <p class="tech">${TYPE[p.type]} · начало ${dt(p.window_start)} · конец ${dt(p.window_end)} · задержка ${p.grace_s} с · номер ${esc(p.id)}</p>`;
  if (!final) share(pub, pub.tunnel ? 'tunnel' : 'lan', p.id);
  const fin = $('#fin'), del = $('#del'), act = $('#act');
  if (fin) {
    live(p, p.status);
    fin.onclick = async () => {
      if (!await sure(g, 'Досрочное завершение', 'Голосование закончится сейчас. Зрители с отстающей трансляцией ещё успеют проголосовать в пределах задержки. Отменить нельзя. Завершить?', 'Завершить', 'danger')) return;
      fin.disabled = true;
      try { await api('POST', `/polls/${p.id}/finish`); toast('Голосование завершено.', true); }
      catch (e) { if (e.status === 401) return; toast(e.status === 409 ? 'Не удалось завершить: голосование уже закончилось.' : e.message); }
      route();
    };
  }
  if (del) del.onclick = () => remove(p, del, g);
  // Туннель закрыт — при запуске предупреждаем и об интернете: «Да» открывает туннель, «Нет» оставляет опрос в локальной сети.
  if (act) act.onclick = async () => {
    const fixed = 'После запуска опрос нельзя изменить или удалить до завершения.';
    const how = pub.tunnel ? (await ask('Запуск опроса', fixed + ' Запустить?', 'Запустить') && 'run')
      : await dialog('Запуск опроса', [fixed,
          'Страница голосования откроется из интернета через Cloudflare-туннель, чтобы голосовать мог любой телефон. Админка останется только на этом компьютере.'],
          [['net', 'Да, продолжить'], ['run', 'Нет, только в локальной сети', 'ghost']]);
    if (!how || !stale(g)) return;
    act.disabled = true;
    try { await api('POST', `/polls/${p.id}/activate`); toast('Опрос запущен.', true); }
    catch (e) {
      if (e.status === 401) return;
      if (e.status !== 409) { toast(e.message); act.disabled = false; return; }
      // 409 в двух случаях: опрос уже не черновик или время прошло. Различаем, перечитав опрос.
      const q = await api('GET', '/polls/' + p.id).catch(() => null);
      toast(q && q.status !== 'draft' ? 'Опрос уже запущен.' : 'Не удалось запустить: время голосования уже прошло.');
      return route();
    }
    if (how === 'net') await setNet(true);
    route();
  };
  // В draft голосов нет — аналитику не спрашиваем.
  if (!draft) loop(p, g);
}

// Плашка и кнопка «Завершить досрочно» у запущенного опроса. finish принимается только внутри окна
// (window_start ≤ сейчас < window_end), поэтому кнопку показываем только тогда.
function live(p, status) {
  if (!$('#notice')) return;
  const now = Date.now(), start = new Date(p.window_start), can = status === 'active' && now >= start && now < new Date(p.window_end);
  // время начала: сегодня — только часы, иначе с датой; секунды — если они есть
  const at = start.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', ...(start.getSeconds() && { second: '2-digit' }) });
  const day = dayKey(start) === dayKey(new Date()) ? 'в ' : start.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' }) + ' в ';
  $('#notice').textContent = phase(p, status) === 'planned' ? `Опрос запланирован: голосование начнётся ${day}${at}. Изменить или удалить его нельзя.`
    : 'Опрос запущен: изменить или удалить его нельзя' + (can ? ', но можно завершить досрочно.' : ' до завершения.');
  $('#finrow').hidden = !can;
}

// ---------- аналитика: раз в секунду, пока не final ----------

async function loop(p, g) {
  let final = false;
  try {
    const [res, an] = await Promise.all([api('GET', `/polls/${p.id}/results`), api('GET', `/polls/${p.id}/analytics`)]);
    if (g !== gen) return;
    final = res.status === 'final';
    // опрос завершился у нас на глазах: перерисовать карточку целиком — без ссылки, с кнопкой «Удалить»
    if (final && p.status !== 'final') return route();
    $('#st').innerHTML = badge(p, res.status);
    live(p, res.status);
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
  const f = an.funnel, rj = f.rejected;
  const bar = v => `<div class="track"><div class="fill" style="width:${v * 100}%"></div></div>`;
  // воронка: полосы вплотную, по центру, сужаются сверху вниз
  const step = (label, v) => `<div>${label}</div><div class="cone"><div class="fill" style="width:${v / f.received * 100}%"></div></div><div class="val">${num(v)}</div>`;
  const why = (label, v, hint) => `<div>${label}<span class="hint">${hint}</span></div><div class="val">${num(v)}</div>`;
  return `<div class="kpis">
    <div class="kpi"><div class="big">${num(res.counted)}</div><div class="muted">голосов засчитано</div></div>
    <div class="kpi"><div class="mid">${num(res.received)}</div><div class="muted">получено всего</div></div>
    <div class="kpi"><div class="mid">${pct(res.over_limit_share)}</div><div class="muted">отсеяно как подозрительные</div></div>
  </div>
  <h2>Итоги</h2>
  ${p.type === 'multi' ? '<p class="muted small note top">Зрители могли выбрать несколько вариантов, поэтому в сумме может быть больше 100%.</p>' : ''}
  <div class="bars">${res.options.map(o => `<div>
    <div class="bar-top"><span>${esc(o.label)}</span><span class="mono">${pct(o.share)}</span></div>
    ${bar(o.share)}
    <div class="mono muted small">${num(o.counted)} засчитано из ${num(o.total)}</div></div>`).join('')}</div>
  <h2>Голоса по секундам</h2>${chart(an, p)}
  ${f.received ? `<h2>Какие голоса засчитаны</h2><div class="funnel">
    ${step('Всего голосов', f.received)}
    ${step('Разные зрители', f.unique_voters)}
    ${step('Засчитано', f.counted)}
  </div>
  <div class="why">
    ${why('Повторные голоса из того же браузера', rj.repeat_voter, 'Засчитываем только первый голос.')}
    ${why('Похожие устройства сверх лимита', rj.key_limit, 'С одного адреса голосовало слишком много одинаковых устройств.')}
    ${why('Слишком много голосов с одного адреса', rj.ip_ceiling, 'Больше 5000 голосов в минуту.')}
  </div>` : ''}`;
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
  const tl = an.timeline, m = an.model;
  const W = Math.max(320, main.clientWidth - 64), H = 260, L = 64, R = 24, T = 16, B = 32;
  const win = (new Date(p.window_end) - new Date(p.window_start)) / 1000;
  const n = Math.max(tl.length, Math.ceil(win + p.grace_s), 1);
  // Столбцов не больше, чем точек по ширине: в столбце k секунд. Высота — голосов за секунду (сумма в столбце / k).
  const k = Math.ceil(n / (W - L - R)), cols = Math.ceil(n / k);
  const bars = Array(Math.ceil(tl.length / k)).fill(0);
  let total = 0;
  for (const d of tl) { bars[Math.floor(d.t / k)] += d.received; total += d.received; }
  // Ожидаемое по модели: в эфире — из пришедших голосов на прошедшую часть окна, до текущего момента; в final — на весь итог.
  const el = an.status === 'final' ? Infinity : (Date.now() - new Date(p.window_start)) / 1000, part = cdf(el, m);
  const model = [];
  for (let i = 0; i < cols && (i + 1) * k <= el && part > 0; i++) model.push(total * (cdf((i + 1) * k, m) - cdf(i * k, m)) / part);
  const max = [...bars, ...model].reduce((a, v) => Math.max(a, v / k), 1);
  const ys = Math.max(1, nice(max / 4)), top = Math.ceil(max / ys) * ys, xs = Math.max(1, nice(n / Math.max(3, W / 110)));
  const bw = (W - L - R) / cols;
  const x = t => L + t / k * bw, y = v => T + (1 - v / top) * (H - T - B);
  let s = '';
  for (let v = 0; v <= top; v += ys) s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${short(v)}</text>`;
  for (let t = 0; t <= n; t += xs) s += `<text x="${x(t)}" y="${H - 10}" text-anchor="middle">${t} с</text>`;
  const gap = bw > 4 ? 1 : 0;
  bars.forEach((v, i) => {
    const a = i * k, h = y(v / k);
    s += `<g><rect class="bar" x="${x(a) + gap}" y="${h}" width="${bw - 2 * gap}" height="${y(0) - h}"/>`
      + `<line class="top" x1="${x(a) + gap}" x2="${x(a + k) - gap}" y1="${h}" y2="${h}"/>`
      + `<rect class="hit" x="${x(a)}" y="${T}" width="${bw}" height="${H - T - B}"><title>${a}–${a + k} с: ${num(v)} голосов, ожидали ${num(model[i] && Math.round(model[i]))}</title></rect></g>`;
  });
  if (win < n) s += `<line class="end" x1="${x(win)}" x2="${x(win)}" y1="${T}" y2="${y(0)}"/><text x="${x(win) + 6}" y="${T + 10}">конец</text>`;
  s += `<polyline class="model" points="${model.map((v, i) => `${x((i + .5) * k).toFixed(1)},${y(v / k).toFixed(1)}`).join(' ')}"/>`;
  return `<div class="legend"><span><i class="key"></i>голосов за секунду</span><span><i class="key line"></i>ожидали</span></div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Голоса по секундам">${s}</svg>`;
}

route();
