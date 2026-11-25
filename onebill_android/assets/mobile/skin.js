/* OneBill Android skin. Injected by the Flutter shell after each page load.
   It rearranges and styles what the engine already shows. Every action
   calls the original link or button, so permissions and rules stay in the engine. */
(function () {
  if (window.__obSkin) return;
  window.__obSkin = true;
  document.documentElement.classList.add('ob-mobile');

  var MAIN = ['pos', 'prod', 'inv', 'rep'];
  var ICONS = { pos: '🧾', prod: '📦', inv: '📄', rep: '📊', par: '👥', pur: '🛒', exp: '💸', acc: '🏦', doc: '🗂️', led: '🎯', set: '⚙️' };
  var cartOpen = false, cartCard = null, nav, top_, sheet, scrim, cartBtn, grid;

  function $(s) { return document.querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function curTab() { return typeof cur !== 'undefined' ? cur : 'pos'; }
  function shown(el) { return !!el && getComputedStyle(el).display !== 'none'; }
  function label(a) { return a.textContent.replace(/\s*\d+\s*$/, '').trim(); }
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }

  function closeAll() {
    cartOpen = false;
    if (cartCard) cartCard.classList.remove('ob-open');
    if (sheet) sheet.classList.remove('on');
    if (scrim) scrim.classList.remove('on');
  }
  function openCart() {
    if (!cartCard) return;
    cartOpen = true; cartCard.classList.add('ob-open'); scrim.classList.add('on'); sheet.classList.remove('on');
  }
  function openMore() {
    closeAll(); sheet.classList.add('on'); scrim.classList.add('on');
  }

  function ensureUI() {
    if ($('#ob-nav')) return;
    top_ = el('div', '', '<b>OneBill</b><span id="ob-title"></span>'); top_.id = 'ob-top';
    nav = el('nav'); nav.id = 'ob-nav';
    scrim = el('div'); scrim.id = 'ob-scrim'; scrim.onclick = closeAll;
    sheet = el('div'); sheet.id = 'ob-more';
    sheet.innerHTML = '<div class="ob-grip"></div><h3>More</h3>';
    grid = el('div', 'ob-grid'); sheet.appendChild(grid);
    cartBtn = el('button'); cartBtn.id = 'ob-cartbtn'; cartBtn.onclick = openCart;

    var main = $('main') || document.body.firstElementChild;
    document.body.insertBefore(top_, document.body.firstChild);
    document.body.appendChild(nav);
    document.body.appendChild(scrim);
    document.body.appendChild(sheet);
    document.body.appendChild(cartBtn);

    var lines = $('#lines');
    var card = lines ? (lines.closest('.card') || lines.parentElement) : null;
    var tiles = $('#tiles');
    if (card && !(tiles && card.contains(tiles))) {
      cartCard = card;
      cartCard.classList.add('ob-cart');
      cartCard.classList.remove('rv');
      var close = el('button', 'ob-cart-close', 'Close cart');
      close.onclick = closeAll;
      cartCard.appendChild(close);
      document.body.appendChild(cartCard);
    }
  }

  function render() {
    ensureUI();
    var links = $$('#links a[data-t]').filter(function (a) { return shown(a); });
    var main = [], more = [];
    links.forEach(function (a) { (MAIN.indexOf(a.dataset.t) >= 0 ? main : more).push(a); });
    var c = curTab();

    nav.innerHTML = '';
    main.forEach(function (a) {
      var b = el('button', 'ob-tab' + (c === a.dataset.t ? ' on' : ''),
        '<i>' + (ICONS[a.dataset.t] || '•') + '</i><span>' + label(a) + '</span>');
      b.onclick = function () { a.click(); };
      nav.appendChild(b);
    });
    var moreOn = more.some(function (a) { return a.dataset.t === c; }) || c === 'more';
    var mb = el('button', 'ob-tab' + (moreOn ? ' on' : ''), '<i>⋯</i><span>More</span>');
    mb.onclick = openMore;
    nav.appendChild(mb);

    grid.innerHTML = '';
    more.forEach(function (a) {
      var t = el('button', 'ob-tile', '<i>' + (ICONS[a.dataset.t] || '•') + '</i><span>' + label(a) + '</span>');
      t.onclick = function () { a.click(); };
      grid.appendChild(t);
    });
    var lb = $('#lockbtn');
    if (lb) {
      var lk = el('button', 'ob-tile', '<i>🔒</i><span>Lock</span>');
      lk.onclick = function () { lb.click(); };
      grid.appendChild(lk);
    }
    var active = $('#links a.on');
    $('#ob-title').textContent = active ? label(active) : '';
    updateCart();
  }

  function updateCart() {
    if (!cartBtn) return;
    var lines = $('#lines');
    var n = lines ? $$('#lines > *').length : 0;
    var tot = $('#tot');
    var total = tot ? tot.textContent.trim() : '';
    cartBtn.innerHTML = '<span>Cart · ' + n + ' item' + (n === 1 ? '' : 's') + '</span><b>' + total + '</b>';
    cartBtn.classList.toggle('ob-show', !!cartCard && curTab() === 'pos' && n > 0 && !cartOpen);
  }

  function labelTables() {
    $$('table').forEach(function (t) {
      if (t.closest('#ob-nav,#ob-more,#ob-cartbtn')) return;
      var labels = $$('thead th', t).map(function (th) { return th.textContent.trim(); });
      $$('tr', t).forEach(function (tr) {
        $$('td', tr).forEach(function (td, i) {
          var lab = labels[i] || '';
          if (lab && td.getAttribute('data-label') !== lab) td.setAttribute('data-label', lab);
        });
      });
      if (!t.classList.contains('ob-cards')) t.classList.add('ob-cards');
    });
  }

  // Wrap navigation so the bars follow the active tab.
  var originalGo = window.go;
  if (typeof originalGo === 'function') {
    window.go = function () {
      var r = originalGo.apply(this, arguments);
      closeAll();
      setTimeout(render, 60);
      return r;
    };
  }

  // Android back button: close a sheet first, then leave the sale screen, then exit.
  window.obBack = function () {
    if (cartOpen || (sheet && sheet.classList.contains('on'))) { closeAll(); return true; }
    if (curTab() !== 'pos' && $('#links a[data-t="pos"]') && shown($('#links a[data-t="pos"]'))) {
      window.go('pos'); return true;
    }
    return false;
  };

  var timer = null;
  new MutationObserver(function (records) {
    var real = records.some(function (r) {
      var t = r.target;
      return !(t.closest && t.closest('#ob-top,#ob-nav,#ob-scrim,#ob-more,#ob-cartbtn'));
    });
    if (!real) return;
    clearTimeout(timer);
    timer = setTimeout(function () { labelTables(); render(); }, 80);
  }).observe(document.body, { childList: true, subtree: true, characterData: true });

  render();
  labelTables();
})();
