// Отпечаток браузера: объект стабильных компонентов по docs/fingerprint.md.
// Хэш не считаем: fp_hash считает приём. Что брать, решаем по семейству браузера, а не по режиму.
// Семейства: chrome (Chromium без Brave), brave, firefox (Gecko), rfp (Gecko с RFP-заглушкой canvas),
// safari (WebKit: Safari и все браузеры на iOS).

// STUB: выбор движка для всех браузеров на iOS (Chrome, Firefox, Telegram — это WebKit) — правила Safari.
// В docs сказано только «Safari»; вопрос в отчёте.

// STUB: список шрифтов в docs не задан. Взят короткий набор распространённых шрифтов Windows/macOS/Android/Linux.
var FONTS = ['Arial', 'Arial Unicode MS', 'Calibri', 'Cambria', 'Comic Sans MS', 'Consolas', 'Courier New',
  'DejaVu Sans', 'Georgia', 'Helvetica Neue', 'Impact', 'Liberation Sans', 'Menlo', 'Microsoft YaHei',
  'Monaco', 'MS Gothic', 'Noto Sans', 'PT Sans', 'Roboto', 'Segoe UI', 'Tahoma', 'Times New Roman',
  'Ubuntu', 'Verdana', 'Yu Gothic'];

// STUB: какие лимиты WebGL брать, в docs не задано. Взяты базовые лимиты WebGL 1.
var GL_LIMITS = ['MAX_TEXTURE_SIZE', 'MAX_CUBE_MAP_TEXTURE_SIZE', 'MAX_RENDERBUFFER_SIZE', 'MAX_VIEWPORT_DIMS',
  'MAX_VERTEX_ATTRIBS', 'MAX_VERTEX_UNIFORM_VECTORS', 'MAX_FRAGMENT_UNIFORM_VECTORS', 'MAX_VARYING_VECTORS',
  'MAX_TEXTURE_IMAGE_UNITS', 'MAX_VERTEX_TEXTURE_IMAGE_UNITS', 'MAX_COMBINED_TEXTURE_IMAGE_UNITS',
  'ALIASED_LINE_WIDTH_RANGE', 'ALIASED_POINT_SIZE_RANGE'];

// Первое значение медиазапроса из списка, которое совпало.
function mq(name, values) {
  for (var i = 0; i < values.length; i++) {
    if (matchMedia('(' + name + ': ' + values[i] + ')').matches) return values[i];
  }
  return null;
}

// Тип защиты по эталонной шахматке 8×8 (по пикселю на клетку, без сглаживания), docs/fingerprint.md.
var COLORS = [[10, 200, 90], [240, 30, 160]];
function protectionType() {
  if (navigator.brave) return 'brave';
  var c = document.createElement('canvas');
  c.width = c.height = 8;
  var x = c.getContext('2d');
  if (!x) return 'no-canvas'; // STUB: класса для «canvas недоступен» в docs нет
  for (var i = 0; i < 64; i++) {
    var col = COLORS[(i + (i >> 3)) % 2];
    x.fillStyle = 'rgb(' + col.join(',') + ')';
    x.fillRect(i % 8, i >> 3, 1, 1);
  }
  var a, b;
  try {
    a = x.getImageData(0, 0, 8, 8).data;
    b = x.getImageData(0, 0, 8, 8).data;
  } catch (e) {
    return 'no-canvas'; // STUB: класса для «чтение canvas запрещено» в docs нет
  }
  var ref = true, white = true, periodic = true, same = true;
  for (var j = 0; j < 256; j++) {
    var p = j >> 2, k = j & 3;
    var want = k === 3 ? 255 : COLORS[(p + (p >> 3)) % 2][k];
    if (a[j] !== want) ref = false;
    if (a[j] !== 255) white = false;
    if (j >= 32 && a[j] !== a[j - 32]) periodic = false; // период 8 пикселей = 32 байта
    if (a[j] !== b[j]) same = false;
  }
  if (ref) return 'none';
  if (white) return 'white'; // STUB: «белая заглушка» не входит в 4 класса из docs (нет/RFP/Brave/расширение)
  // Упрощение: «не зависит от нарисованного» не проверяем вторым рисунком, хватает периода 8 и несовпадения с эталоном.
  if (periodic) return 'rfp';
  if (!same) return 'extension';
  // STUB: не эталон, но стабильно при двух чтениях (так выглядят Safari/Firefox FPP в приватном окне,
  // или детерминированное расширение в Chrome). Класса в docs нет.
  return 'other';
}

function webgl() {
  var gl = document.createElement('canvas').getContext('webgl');
  if (!gl) return null;
  var dbg = gl.getExtension('WEBGL_debug_renderer_info');
  var limits = {};
  GL_LIMITS.forEach(function (n) {
    var v = gl.getParameter(gl[n]);
    limits[n] = v && v.length ? Array.from(v) : v;
  });
  return {
    vendor: gl.getParameter(dbg ? dbg.UNMASKED_VENDOR_WEBGL : gl.VENDOR),
    renderer: gl.getParameter(dbg ? dbg.UNMASKED_RENDERER_WEBGL : gl.RENDERER),
    limits: limits,
    extensions: (gl.getSupportedExtensions() || []).slice().sort()
  };
}

// Хэш canvas 2D как компонент (не хэш всего fp).
async function canvasHash() {
  // STUB: что рисовать, в docs не задано. Текст со шрифтами, эмодзи и полупрозрачностью, как у FingerprintJS.
  var c = document.createElement('canvas');
  c.width = 240;
  c.height = 60;
  var x = c.getContext('2d');
  x.textBaseline = 'top';
  x.fillStyle = '#f60';
  x.fillRect(100, 1, 62, 20);
  x.font = '14px Arial';
  x.fillStyle = '#069';
  x.fillText('Cwm fjordbank gly 😃', 2, 15);
  x.font = '18px serif';
  x.fillStyle = 'rgba(102, 204, 0, 0.7)';
  x.fillText('Съешь же ещё', 4, 35);
  var h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(c.toDataURL()));
  return Array.from(new Uint8Array(h).slice(0, 8), function (v) { return v.toString(16).padStart(2, '0'); }).join('');
}

// OfflineAudioContext: треугольник 10 кГц через компрессор, сумма модулей отсчётов 4500..5000 (как у FingerprintJS).
async function audio() {
  var ctx = new OfflineAudioContext(1, 5000, 44100);
  var o = ctx.createOscillator();
  o.type = 'triangle';
  o.frequency.value = 10000;
  var comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -50;
  comp.knee.value = 40;
  comp.ratio.value = 12;
  comp.attack.value = 0;
  comp.release.value = 0.25;
  o.connect(comp);
  comp.connect(ctx.destination);
  o.start(0);
  var s = (await ctx.startRendering()).getChannelData(0);
  var sum = 0;
  for (var i = 4500; i < 5000; i++) sum += Math.abs(s[i]);
  return sum;
}

// Какие шрифты из списка есть: ширина текста отличается от запасного шрифта.
function fonts(list) {
  var x = document.createElement('canvas').getContext('2d');
  var t = 'mmmmmmmmmmlli10WQ';
  function w(f) { x.font = '72px ' + f; return x.measureText(t).width; }
  var mono = w('monospace'), serif = w('serif');
  return list.filter(function (f) {
    return w('"' + f + '", monospace') !== mono || w('"' + f + '", serif') !== serif;
  });
}

async function getFp() {
  var ua = navigator.userAgent;
  var ios = /iPhone|iPad|iPod/.test(ua);
  var engine = /\bGecko\/\d/.test(ua) ? 'gecko' : /Chrome\/\d/.test(ua) && !ios ? 'blink' : 'webkit';
  var os = ios ? 'ios' : /Android/.test(ua) ? 'android' : /CrOS/.test(ua) ? 'chromeos' : /Windows/.test(ua) ? 'windows'
    : /Mac OS X/.test(ua) ? 'macos' : /Linux/.test(ua) ? 'linux' : 'other';

  // Браузер и мажорная версия. Safari: Version/26.x (версия iOS в UA заморожена).
  var browser = 'other', version = null;
  var list = [['samsung', /SamsungBrowser\/(\d+)/], ['yandex', /YaBrowser\/(\d+)/], ['edge', /Edg\w*\/(\d+)/],
    ['opera', /OPR\/(\d+)/], ['chrome-ios', /CriOS\/(\d+)/], ['firefox-ios', /FxiOS\/(\d+)/],
    ['firefox', /Firefox\/(\d+)/], ['chrome', /Chrome\/(\d+)/], ['safari', /Version\/(\d+)/]];
  for (var i = 0; i < list.length; i++) {
    var m = ua.match(list[i][1]);
    if (m) { browser = list[i][0]; version = +m[1]; break; }
  }
  if (navigator.brave) browser = 'brave';
  // STUB: признаки in-app браузеров в docs не перечислены. Взяты известные метки UA и объект Telegram.
  var app = ua.match(/Telegram|VKAndroidApp|VKClient|Instagram|FBAN|FBAV|; wv\)/);
  var inApp = window.TelegramWebviewProxy ? 'telegram' : app ? app[0] : null;

  var protection = protectionType();
  var family = browser === 'brave' ? 'brave' : engine === 'blink' ? 'chrome'
    : engine === 'gecko' ? (protection === 'rfp' ? 'rfp' : 'firefox') : 'safari';
  // STUB: «Chrome» в строках «Экран», «WebGL», «Шрифты» понят как Chromium без Brave (Samsung, Edge, Yandex, Opera тоже).
  var chrome = family === 'chrome';

  var fp = { os: os, engine: engine, browser: browser, version: version, in_app: inApp };

  // Модель устройства: Chromium на Android (Brave и Samsung тоже, если отдают).
  if (engine === 'blink' && os === 'android' && navigator.userAgentData) {
    fp.model = (await navigator.userAgentData.getHighEntropyValues(['model'])).model;
  }

  // Экран: только Chrome. iPad и Mac Safari, RFP, Brave, Firefox — не берём.
  if (chrome) {
    fp.screen = [Math.min(screen.width, screen.height), Math.max(screen.width, screen.height), devicePixelRatio];
  }
  // STUB: iPhone — «свести к 4 размерам во всех режимах». Какие это 4 размера и как к ним сводить, в docs нет.
  // Пока экран iPhone не берём.

  fp.color = [screen.colorDepth, mq('color-gamut', ['rec2020', 'p3', 'srgb']), mq('dynamic-range', ['high', 'standard'])];

  var langs = navigator.languages || [navigator.language];
  fp.languages = family === 'brave' ? langs.slice(0, 1) : langs;
  var dtf = new Intl.DateTimeFormat().resolvedOptions();
  fp.intl = [dtf.locale, dtf.calendar, new Intl.DateTimeFormat(undefined, { hour: 'numeric' }).resolvedOptions().hourCycle];
  fp.timezone = dtf.timeZone;

  if (family !== 'brave') {
    var hc = navigator.hardwareConcurrency;
    // STUB: Firefox «свести к 4/8» — порог (>= 8 → 8, иначе 4) в docs не задан.
    fp.cores = engine === 'gecko' ? (hc >= 8 ? 8 : 4) : hc;
    fp.memory = navigator.deviceMemory; // есть только в Chromium; undefined в JSON не попадает
  }

  if (chrome || family === 'firefox') fp.webgl = webgl();

  // STUB: canvas и audio берём только при protection === 'none': при шуме расширения значение меняется на каждом
  // чтении. В docs этого условия нет.
  if (chrome && protection === 'none') fp.canvas = await canvasHash();
  // STUB: audio — «только Chrome» понято буквально: только browser === 'chrome' (без Samsung, Edge, Yandex, Opera).
  if (browser === 'chrome' && protection === 'none') {
    // STUB: таймаут 1 с — чтобы подсчёт не завис (например, в фоновой вкладке). В docs не задан.
    fp.audio = await Promise.race([audio(), new Promise(function (r) { setTimeout(r, 1000, 'timeout'); })]);
  }

  if (chrome) fp.fonts = fonts(FONTS);
  // STUB: Firefox — «только системные шрифты»: список системных шрифтов по ОС в docs нет, пока шрифты в Firefox не берём.

  // Тема входит в отпечаток (решение автора в docs).
  fp.media = [
    mq('prefers-color-scheme', ['dark', 'light']),
    mq('prefers-reduced-motion', ['reduce', 'no-preference']),
    mq('prefers-contrast', ['more', 'less', 'custom', 'no-preference']),
    mq('inverted-colors', ['inverted', 'none']),
    mq('forced-colors', ['active', 'none']),
    mq('prefers-reduced-transparency', ['reduce', 'no-preference'])
  ];

  if (family === 'safari') {
    var el = document.createElement('div');
    el.style.font = '-apple-system-body';
    document.body.appendChild(el);
    fp.apple_font = getComputedStyle(el).fontSize;
    el.remove();
  }

  // Тип защиты: не для Safari и Firefox. STUB: для семейства rfp (это тоже Gecko) берём — в docs неоднозначно.
  if (family !== 'safari' && family !== 'firefox') fp.protection = protection;

  return fp;
}
