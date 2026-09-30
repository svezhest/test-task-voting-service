// Отпечаток браузера: белый список из docs/fingerprint.md, одинаково для всех браузеров.
// Хэш не считаем: fp_hash считает приём.

// Первое значение медиазапроса из списка, которое совпало.
function mq(name, values) {
  return values.find(function (v) { return matchMedia('(' + name + ': ' + v + ')').matches; }) || null;
}

async function getFp() {
  var ua = navigator.userAgent;
  var ios = /iPhone|iPad|iPod/.test(ua);
  var fp = {
    os: ios ? 'ios' : /Android/.test(ua) ? 'android' : /CrOS/.test(ua) ? 'chromeos' : /Windows/.test(ua) ? 'windows'
      : /Mac OS X/.test(ua) ? 'macos' : /Linux/.test(ua) ? 'linux' : 'other',
    // Все браузеры на iOS — WebKit.
    engine: /\bGecko\/\d/.test(ua) ? 'gecko' : /Chrome\/\d/.test(ua) && !ios ? 'blink' : 'webkit',
    browser: 'other',
    version: null
  };

  // Браузер и мажорная версия. Safari: Version/NN (версия ОС в UA заморожена).
  var list = [['samsung', /SamsungBrowser\/(\d+)/], ['yandex', /YaBrowser\/(\d+)/], ['edge', /Edg\w*\/(\d+)/],
    ['opera', /OPR\/(\d+)/], ['chrome-ios', /CriOS\/(\d+)/], ['firefox-ios', /FxiOS\/(\d+)/],
    ['firefox', /Firefox\/(\d+)/], ['chrome', /Chrome\/(\d+)/], ['safari', /Version\/(\d+)/]];
  for (var i = 0; i < list.length; i++) {
    var m = ua.match(list[i][1]);
    if (m) { fp.browser = list[i][0]; fp.version = +m[1]; break; }
  }
  if (navigator.brave) fp.browser = 'brave'; // UA у Brave как у Chrome

  // STUB: признаки in-app браузеров в docs не перечислены. Взяты известные метки UA и объект Telegram.
  var app = ua.match(/Telegram|VKAndroidApp|VKClient|Instagram|FBAN|FBAV|; wv\)/);
  fp.in_app = window.TelegramWebviewProxy ? 'telegram' : app ? app[0] : null;

  // Модель: только если браузер её отдаёт; пустую строку (десктоп) не берём.
  try {
    var model = navigator.userAgentData && (await navigator.userAgentData.getHighEntropyValues(['model'])).model;
    if (model) fp.model = model;
  } catch (e) {}

  fp.display = [devicePixelRatio, window.screen.colorDepth,
    mq('color-gamut', ['rec2020', 'p3', 'srgb']), mq('dynamic-range', ['high', 'standard'])];

  fp.language = (navigator.languages && navigator.languages[0]) || navigator.language;
  var dtf = new Intl.DateTimeFormat().resolvedOptions();
  fp.intl = [dtf.locale, dtf.calendar, new Intl.DateTimeFormat(undefined, { hour: 'numeric' }).resolvedOptions().hourCycle];
  fp.timezone = dtf.timeZone;

  // Тема входит в отпечаток (решение автора в docs).
  fp.media = [
    mq('prefers-color-scheme', ['dark', 'light']),
    mq('prefers-reduced-motion', ['reduce', 'no-preference']),
    mq('prefers-contrast', ['more', 'less', 'custom', 'no-preference']),
    mq('inverted-colors', ['inverted', 'none']),
    mq('forced-colors', ['active', 'none']),
    mq('prefers-reduced-transparency', ['reduce', 'no-preference'])
  ];

  // Размер шрифта -apple-system-body. Где ключевого слова нет, остаётся medium,
  // чтобы значение не зависело от CSS страницы.
  var el = document.createElement('div');
  el.style.fontSize = 'medium';
  el.style.font = '-apple-system-body';
  document.body.append(el);
  fp.apple_font = getComputedStyle(el).fontSize;
  el.remove();

  return fp;
}
