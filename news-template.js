// news-template.js
// Shared rendering engine for all news category pages.
// Each news page loads this file, then calls window.renderNewsPage(config).
//
// config = {
//   data:       object   — the NEWS data variable (e.g. WORLD_NEWS)
//   varName:    string   — name of the variable, used in empty-state message
//   dataFile:   string   — filename of the data JS, used in empty-state message
//   icon:       string   — emoji used as hero/card placeholder and empty-state icon
//   accentVar:  string   — CSS variable name for accent colour, e.g. '--blue'
//   accentHex:  string   — fallback hex for the accent (used in drop-cap colour)
// }

(function () {

  // ── Date label ─────────────────────────────────────────────────────────────
  var dateEl = document.getElementById('js-date');
  if (dateEl) {
    dateEl.textContent = new Date().toLocaleDateString('en-GB', {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
    });
  }

  // ── Ticker — populated from this page's live data ─────────────────────────
  function _initTicker(data) {
    var t = document.getElementById('js-ticker');
    if (!t) return;
    var headlines = [];
    if (data) {
      if (data.main && data.main.title) headlines.push(data.main.title);
      if (Array.isArray(data.secondary)) {
        data.secondary.forEach(function (s) { if (s.title) headlines.push(s.title); });
      }
    }
    if (!headlines.length) headlines = ['Loading latest headlines\u2026'];
    var mk = function (a) {
      return a.map(function (x) {
        return '<span style="color:#fff;font-family:\'Source Serif 4\',serif;font-size:0.78rem;font-weight:600;padding:0 2rem;">'
          + x.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          + '</span><span class="ticker-sep">&#124;</span>';
      }).join('');
    };
    t.innerHTML = mk(headlines) + mk(headlines);
    requestAnimationFrame(function () {
      t.style.animationDuration = (t.scrollWidth / 2 / 60) + 's';
    });
  }

  // ── Dropdown nav ───────────────────────────────────────────────────────────
  (function () {
    var btns = document.querySelectorAll('.nav-drop-btn');
    btns.forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var menu = btn.nextElementSibling;
        var isOpen = menu.style.display === 'flex';
        document.querySelectorAll('.nav-drop-menu').forEach(function (m) { m.style.display = ''; });
        if (!isOpen) menu.style.display = 'flex';
      });
    });
    document.addEventListener('click', function () {
      document.querySelectorAll('.nav-drop-menu').forEach(function (m) { m.style.display = ''; });
    });
  })();

  // ── Render engine ──────────────────────────────────────────────────────────
  window.renderNewsPage = function (cfg) {
    var output = document.getElementById('news-output');
    if (!output) return;

    _initTicker(cfg.data);

    if (!cfg.data) {
      output.innerHTML =
        '<div class="empty-state">'
        + '<span class="empty-icon">' + cfg.icon + '</span>'
        + '<div class="empty-title">No news available</div>'
        + '<p class="empty-text">Update <code>' + cfg.dataFile + '</code> with today\'s stories.</p>'
        + '</div>';
      return;
    }

    var data = cfg.data;
    var accent = cfg.accentVar || '--blue';
    var accentHex = cfg.accentHex || '#003399';

    // Helpers
    function esc(s) {
      return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function fmtDate(iso) {
      return new Date(iso).toLocaleDateString('en-GB', {
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
      });
    }

    function imgOrPlaceholder(src, alt, wrapCls, icon) {
      if (!src) return '<div class="' + wrapCls + '-placeholder">' + icon + '</div>';
      return '<img src="' + esc(src) + '" alt="' + esc(alt) + '" loading="lazy" '
        + 'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
        + '<div class="' + wrapCls + '-placeholder" style="display:none">' + icon + '</div>';
    }

    function catPill(cat) {
      if (!cat) return '';
      return '<span class="story-tag">' + esc(cat) + '</span>';
    }

    // Main story
    var m = data.main;
    var paras = Array.isArray(m.content) ? m.content : m.content.split(/\n+/).filter(Boolean);
    var pullQuote = paras[0].split('.')[0] + '.';

    var mainHTML =
      '<article class="main-story">'
      + '<div class="story-eyebrow">'
      +   (m.category ? catPill(m.category) : '<span class="story-tag">Main Story</span>')
      +   '<time class="story-date">' + fmtDate(data.date) + '</time>'
      + '</div>'
      + '<h2 class="story-headline">' + esc(m.title) + '</h2>'
      + '<div class="hero-img-wrap">' + imgOrPlaceholder(m.image, m.title, 'hero-placeholder', cfg.icon) + '</div>'
      + '<div class="story-body">'
      +   '<div class="story-text">'
      +     paras.map(function (p) { return '<p>' + esc(p) + '</p>'; }).join('')
      +   '</div>'
      +   '<aside class="story-aside">'
      +     '<blockquote class="pull-quote"><p>&ldquo;' + esc(pullQuote) + '&rdquo;</p></blockquote>'
      +     (m.source
        ? '<div class="story-source"><span style="display:block;font-weight:700;color:var(--muted);margin-bottom:0.3rem">Source</span>'
          + (m.sourceUrl
            ? '<a href="' + esc(m.sourceUrl) + '" target="_blank" rel="noopener">' + esc(m.source) + '</a>'
            : esc(m.source))
          + '</div>'
        : '')
      +   '</aside>'
      + '</div>'
      + '</article>';

    // Secondary grid
    var secondary = data.secondary || [];
    var gridHTML = '';

    if (secondary.length > 0) {
      var cards = secondary.map(function (item, i) {
        return '<article class="news-card" style="animation-delay:' + (i * 120) + 'ms">'
          + '<div class="card-thumb">' + imgOrPlaceholder(item.image, item.title, 'card-thumb', cfg.icon) + '</div>'
          + '<div class="card-body">'
          +   (item.category ? '<span class="card-category">' + esc(item.category) + '</span>' : '')
          +   '<h3 class="card-title">' + esc(item.title) + '</h3>'
          +   '<p class="card-summary">' + esc(item.summary) + '</p>'
          +   '<div class="card-footer">'
          +     (item.source ? '<span class="card-source">' + esc(item.source) + '</span>' : '<span></span>')
          +     (item.url
            ? '<a class="card-read-more" href="' + esc(item.url) + '" target="_blank" rel="noopener">Read more &rarr;</a>'
            : '')
          +   '</div>'
          + '</div>'
          + '</article>';
      }).join('');

      gridHTML =
        '<section class="secondary-section">'
        + '<div class="section-heading">'
        +   '<h2>More Stories</h2>'
        +   '<div class="section-rule"></div>'
        +   '<span class="section-count">' + secondary.length + ' ' + (secondary.length === 1 ? 'story' : 'stories') + '</span>'
        + '</div>'
        + '<div class="news-grid">' + cards + '</div>'
        + '</section>';
    }

    var footerHTML = '<p class="last-updated">Last updated: ' + fmtDate(data.date) + '</p>';

    output.innerHTML = mainHTML + gridHTML + footerHTML;
  };

})();
