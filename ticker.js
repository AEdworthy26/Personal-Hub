// ticker.js — shared news ticker initialisation
// Reads from WORLD_NEWS (world-news-data.js) if available, else shows a placeholder.
// Include world-news-data.js before this file on any page.

(function () {
  var t = document.getElementById('js-ticker');
  if (!t) return;

  var headlines = [];
  if (typeof WORLD_NEWS !== 'undefined' && WORLD_NEWS) {
    if (WORLD_NEWS.main && WORLD_NEWS.main.title) headlines.push(WORLD_NEWS.main.title);
    if (Array.isArray(WORLD_NEWS.secondary)) {
      WORLD_NEWS.secondary.forEach(function (s) { if (s.title) headlines.push(s.title); });
    }
  }
  if (!headlines.length) headlines = ['Loading latest headlines\u2026'];

  var mk = function (arr) {
    return arr.map(function (h) {
      return '<span style="color:#fff;font-family:\'Source Serif 4\',serif;font-size:0.78rem;font-weight:600;padding:0 2rem;">'
        + h.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        + '</span><span class="ticker-sep">&#124;</span>';
    }).join('');
  };

  t.innerHTML = mk(headlines) + mk(headlines);
  requestAnimationFrame(function () {
    t.style.animationDuration = (t.scrollWidth / 2 / 60) + 's';
  });
})();
