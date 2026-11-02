// Data Digest — фронтенд без збірки (vanilla JS).

const state = {
  screen: 'chat',
  chat: { title: 'Новий чат', messages: [], loading: false },
  digest: { side: 'both', data: null, loading: false, error: '' },
  search: { q: '', side: 'all', days: 7, items: [], note: '', error: '', loading: false },
  profile: null
};

const $main = document.getElementById('main');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON_SEND = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';
const ICON_BOT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h16"/></svg>';
const ICON_SEARCH = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>';


// ---------- Навігація ----------
function go(screen) {
  state.screen = screen;
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.screen === screen));
  render();
  if (screen === 'digests') loadDigest();
  if (screen === 'search') loadSearch();
  if (screen === 'profile') loadProfile();
}

function render() {
  const view = { chat: renderChat, digests: renderDigests, search: renderSearch, profile: renderProfile }[state.screen];
  $main.innerHTML = view();
  if (state.screen === 'chat') {
    const input = $main.querySelector('.composer input');
    if (input) input.focus();
    const thread = $main.querySelector('.thread');
    if (thread) window.scrollTo(0, document.body.scrollHeight);
  }
}

function renderRecent() {
  document.getElementById('recent-list').innerHTML = DEMO.recentChats
    .map((t, i) => `<button class="recent-item" data-recent="${i}" title="${esc(t)}">${esc(t)}</button>`)
    .join('');
}

// ---------- Чат ----------
function composer(big) {
  const dis = state.chat.loading ? 'disabled' : '';
  if (big) {
    return `
      <form class="composer big" data-form="chat">
        <label for="composer" class="sr-only">Повідомлення аналітику</label>
        <input id="composer" name="q" autocomplete="off" placeholder="Запитайте про новини, сюжети чи тренди…">
        <div class="composer-bar">
          <div class="row"><span class="mini-pill">Період: 7 днів</span><span class="mini-pill">UA + RU</span></div>
          <button class="send" type="submit" aria-label="Надіслати" ${dis}>${ICON_SEND}</button>
        </div>
      </form>`;
  }
  return `
    <form class="composer small" data-form="chat">
      <label for="composer" class="sr-only">Повідомлення аналітику</label>
      <input id="composer" name="q" autocomplete="off" placeholder="Уточніть або поставте нове питання…">
      <button class="send" type="submit" aria-label="Надіслати" ${dis}>${ICON_SEND}</button>
    </form>`;
}

function renderChat() {
  const { messages, title, loading } = state.chat;
  const header = `
    <header class="chat-header">
      <span class="chat-title">${esc(title)}</span>
      <span class="pill">База: News · оновлюється щодня</span>
    </header>`;

  if (!messages.length) {
    return `
      <div class="chat">${header}
        <div class="empty">
          <h1>Що аналізуємо сьогодні?</h1>
          <div class="empty-inner">
            ${composer(true)}
            <div class="row suggestions">
              ${DEMO.suggestions.map((s) => `<button class="suggestion" data-ask="${esc(s)}">${esc(s)}</button>`).join('')}
            </div>
          </div>
        </div>
      </div>`;
  }

  const items = messages.map((m) => m.role === 'user'
    ? `<div class="msg-user"><div>${esc(m.content)}</div></div>`
    : `<div class="msg-bot">
         <div class="bot-avatar" aria-hidden="true">${ICON_BOT}</div>
         <div class="bot-body">
           <div class="bot-text">${esc(m.content)}</div>
           ${m.sources?.length ? `<div class="row">${m.sources.map((s) =>
             `<a class="source" href="${esc(s.url || '#')}" target="_blank" rel="noopener"><b>${esc(s.platform)}</b>${esc(s.name)}</a>`).join('')}</div>` : ''}
         </div>
       </div>`).join('');

  const typing = loading
    ? `<div class="msg-bot"><div class="bot-avatar" aria-hidden="true">${ICON_BOT}</div><div class="bot-text typing">Аналізую джерела…</div></div>`
    : '';

  return `
    <div class="chat">${header}
      <div class="thread-wrap">
        <div class="thread" aria-live="polite">${items}${typing}</div>
        <div class="composer-dock">
          ${composer(false)}
          <p class="disclaimer">Відповіді базуються на зібраних постах — перевіряйте джерела.</p>
        </div>
      </div>
    </div>`;
}

async function ask(text) {
  const t = (text || '').trim();
  if (!t || state.chat.loading) return;
  if (state.screen !== 'chat') { state.screen = 'chat'; go('chat'); }
  if (!state.chat.messages.length) state.chat.title = t.length > 48 ? t.slice(0, 48) + '…' : t;
  state.chat.messages.push({ role: 'user', content: t });
  state.chat.loading = true;
  render();
  try {
    const history = state.chat.messages.map(({ role, content }) => ({ role, content }));
    const res = await api.askAnalyst(history);
    state.chat.messages.push({ role: 'assistant', content: res.content, sources: res.sources || [] });
  } catch (e) {
    state.chat.messages.push({ role: 'assistant', content: 'Не вдалося отримати відповідь: ' + e.message });
  } finally {
    state.chat.loading = false;
    if (state.screen === 'chat') render();
  }
}

function newChat() {
  state.chat = { title: 'Новий чат', messages: [], loading: false };
  go('chat');
}

// ---------- Дайджести ----------
const SIDES = [['both', 'Усе'], ['ua', 'Українські джерела'], ['ru', 'Російські джерела'], ['topics', 'Ваші теми й інтереси']];
const SIDE_LABEL = { ua: 'Українські джерела', ru: 'Російські джерела', topics: 'Ваші теми й інтереси' };
const SIDE_NOTE = { ru: 'Заяви російських каналів, а не перевірені факти.' };
// Емодзі з назв розділів пайплайна: на ноутбуці директора вони сірі квадрати
const noEmoji = (s) => String(s || '').replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu, '').trim();

function host(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return 'джерело'; }
}

function sourcesHtml(urls) {
  return (urls || []).slice(0, 6).map((u) =>
    `<a class="source" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(host(u))}</a>`).join('');
}

function whyHtml(why) {
  return (why || []).slice(0, 2).map((w) => `<span class="tag accent">${esc(noEmoji(w))}</span>`).join('');
}

function itemHtml(it, side) {
  const ask = 'Розкажи детальніше про #' + it.ref + ': ' + it.text.slice(0, 160);
  return `
    <article class="story">
      <div class="row">
        <span class="tag">#${esc(it.ref)}</span>
        ${it.is_russian_source_claim ? '<span class="tag high">Заява російських каналів</span>' : ''}
        ${whyHtml(it.why)}
      </div>
      <p>${esc(it.text)}</p>
      <div class="story-foot">
        <div class="row">${sourcesHtml(it.source_urls)}</div>
        <button class="btn-outline" data-ask="${esc(ask)}">Запитати аналітика</button>
      </div>
    </article>`;
}

function digestHtml(data) {
  if (!data) return '';
  const blocks = (data.digests || []).map((d) => `
    <section class="stories">
      <h2 class="h2">${esc(SIDE_LABEL[d.side] || d.side)}</h2>
      ${SIDE_NOTE[d.side] ? `<span class="sub">${esc(SIDE_NOTE[d.side])}</span>` : ''}
      ${d.sections.map((s) => `
        ${s.title ? `<div class="section-label">${esc(noEmoji(s.title))}</div>` : ''}
        ${s.items.map((it) => itemHtml(it, d.side)).join('')}`).join('')}
    </section>`).join('');
  const notes = [data.note, (data.missing_sides || []).length ? 'Ще немає: ' + data.missing_sides.map((m) => SIDE_LABEL[m] || m).join(', ') : '']
    .filter(Boolean).map((n) => `<p class="sub">${esc(n)}</p>`).join('');
  return notes + (blocks || '<p class="sub">Дайджесту за цей день немає.</p>');
}

function renderDigests() {
  const d = state.digest;
  const seg = SIDES.map(([id, label]) =>
    `<button class="${d.side === id ? 'on' : ''}" data-side-digest="${id}">${esc(label)}</button>`).join('');
  return `
    <div class="page"><div class="page-inner">
      <div class="head-row">
        <div>
          <h1 class="h1">Дайджест</h1>
          <span class="sub">${d.data?.date ? esc(d.data.date) + ' · ' : ''}упорядковано за вашими цілями та інтересами</span>
        </div>
      </div>
      <div class="segmented" role="group" aria-label="Джерела">${seg}</div>
      ${d.loading ? '<p class="sub">Завантаження…</p>' : d.error ? `<p class="sub">${esc(d.error)}</p>` : digestHtml(d.data)}
    </div></div>`;
}

async function loadDigest() {
  state.digest.loading = true;
  state.digest.error = '';
  if (state.screen === 'digests') render();
  try { state.digest.data = await api.getDigest(state.digest.side); }
  catch (e) { state.digest.data = null; state.digest.error = 'Не вдалося завантажити дайджест: ' + e.message; }
  state.digest.loading = false;
  if (state.screen === 'digests') render();
}

// ---------- Пошук ----------
function chipsHtml(list, current, attr) {
  return list.map(([id, label]) =>
    `<button class="chip ${String(current) === String(id) ? 'on' : ''}" data-${attr}="${id}">${esc(label)}</button>`).join('');
}

function when(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString('uk-UA', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function resultsHtml() {
  const s = state.search;
  if (s.loading) return '<div class="no-results">Пошук…</div>';
  if (s.error) return `<div class="no-results">${esc(s.error)}</div>`;
  if (!s.items.length) return `<div class="no-results">${esc(s.note || 'Нічого не знайдено. Спробуйте інші слова або більше днів.')}</div>`;
  return s.items.map((r) => `
    <div class="result">
      <div class="result-meta">
        <span class="platform">#${esc(r.ref)}</span>
        <span class="src">${r.channel?.username ? '@' + esc(r.channel.username) : 'Telegram'}</span>
        <span class="when">${r.source_side === 'ru' ? 'RU' : 'UA'} · ${esc(when(r.published_at))}</span>
        ${r.is_russian_source_claim ? '<span class="tag high">Заява російських каналів</span>' : ''}
        ${whyHtml(r.why)}
      </div>
      <p>${esc(r.summary)}</p>
      ${r.post_url ? `<div class="row"><a class="source" href="${esc(r.post_url)}" target="_blank" rel="noopener noreferrer">Відкрити пост</a></div>` : ''}
    </div>`).join('');
}

function renderSearch() {
  const s = state.search;
  return `
    <div class="page"><div class="page-inner">
      <h1 class="h1">Пошук по постах</h1>
      <div class="searchbox">
        ${ICON_SEARCH}
        <label for="post-search" class="sr-only">Пошук</label>
        <input id="post-search" value="${esc(s.q)}" autocomplete="off" placeholder="Ключові слова: назва, технологія, компанія…">
      </div>
      <div class="filters">
        <div class="row"><span class="filter-label">Джерела</span>${chipsHtml([['all', 'Усі'], ['ua', 'UA'], ['ru', 'RU']], s.side, 'search-side')}</div>
        <div class="row"><span class="filter-label">Період</span>${chipsHtml([[1, 'Доба'], [7, 'Тиждень'], [30, 'Місяць']], s.days, 'days')}</div>
      </div>
      <span class="sub" id="results-count">${s.q.trim().length >= 2 ? 'Знайдено постів: ' + s.items.length : ''}</span>
      <div class="results" id="results">${resultsHtml()}</div>
    </div></div>`;
}

async function loadSearch() {
  const s = state.search;
  s.error = '';
  if (s.q.trim().length < 2) { s.items = []; s.note = 'Введіть, що шукати.'; }
  else {
    try { const r = await api.searchPosts(s.q, s.side, s.days); s.items = r.results || []; s.note = r.note || ''; }
    catch (e) { s.items = []; s.error = 'Пошук не вдався: ' + e.message; }
  }
  // оновлюємо лише результати, щоб не збивати фокус у полі пошуку
  const box = document.getElementById('results');
  if (box) {
    box.innerHTML = resultsHtml();
    document.getElementById('results-count').textContent = s.q.trim().length >= 2 ? 'Знайдено постів: ' + s.items.length : '';
  }
}

let searchTimer;
function onSearchInput(value) {
  state.search.q = value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadSearch, 350);
}

// ---------- Профіль ----------
const ABOUT_LABEL = {
  summary: 'Коротко про вас', interest: 'Інтереси', intent: 'Над чим ви зараз працюєте', preference: 'Як ви хочете отримувати новини',
  goal: 'Цілі, які ви згадували', tracked: 'Що ви відстежуєте', known: 'Що ви вже знаєте', avoid: 'Чого уникати',
  fact: 'Про вашу роботу', feedback: 'Ваші відгуки'
};
const TOPIC_STATUS = { active: 'відстежується', explored: 'досліджено', expired: 'завершено', stopped: 'зупинено' };

function listHtml(items, empty) {
  return items.length ? `<ul class="plain">${items.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>` : `<span class="sub">${esc(empty)}</span>`;
}

function renderProfile() {
  const p = state.profile;
  if (!p) return '<div class="page"><div class="page-inner narrow"><p class="sub">Завантаження…</p></div></div>';
  if (p.error) return `<div class="page"><div class="page-inner narrow"><p class="sub">${esc(p.error)}</p></div></div>`;
  const topics = (p.topics || []).filter((t) => t.status !== 'stopped');
  const about = Object.entries(p.about_user || {}).filter(([k]) => k !== 'tracked' && k !== 'goal');
  return `
    <div class="page"><div class="page-inner narrow">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 class="h1">Профіль та інтереси</h1>
        <span class="sub">Від цього залежить, що потрапляє у «Для вас» і як відповідає аналітик. Змінювати тут можна буде на наступному кроці; поки — словами в чаті.</span>
      </div>
      <section class="card">
        <h2>Цілі</h2>
        ${listHtml(p.goals || [], 'Цілей ще немає.')}
      </section>
      <section class="card">
        <h2>Теми, які ви відстежуєте</h2>
        ${topics.length ? `<ul class="plain">${topics.map((t) => `<li><b>${esc(t.name)}</b> <span class="sub">· ${esc(TOPIC_STATUS[t.status] || t.status)}${t.expires_at && t.status === 'active' ? ' до ' + esc(new Date(t.expires_at).toLocaleDateString('uk-UA')) : ''}</span></li>`).join('')}</ul>` : '<span class="sub">Ви ще нічого не відстежуєте.</span>'}
      </section>
      ${about.map(([kind, texts]) => `
        <section class="card">
          <h2>${esc(ABOUT_LABEL[kind] || kind)}</h2>
          ${listHtml(texts, '')}
        </section>`).join('')}
      ${(p.top_tags || []).length ? `
      <section class="card">
        <h2>Що ви читаєте найбільше</h2>
        <div class="row">${p.top_tags.slice(0, 12).map((t) => `<span class="tag">${esc(t.tag)}</span>`).join('')}</div>
      </section>` : ''}
    </div></div>`;
}

async function loadProfile() {
  try { state.profile = await api.getProfile(); }
  catch (e) { state.profile = { error: 'Не вдалося завантажити профіль: ' + e.message }; }
  if (state.screen === 'profile') render();
}

// ---------- Користувач ----------
async function loadMe() {
  try {
    const me = await api.me();
    const name = me.name || me.email || 'Ви';
    document.querySelector('.user-name').textContent = name;
    document.querySelector('.user-role').textContent = me.dev ? 'локальний режим' : (me.email || '');
    document.querySelector('.avatar').textContent = name.trim().charAt(0).toUpperCase();
  } catch (e) { console.error(e); }
}

// ---------- Події ----------
document.querySelector('.sidebar').addEventListener('click', (e) => {
  const nav = e.target.closest('[data-screen]');
  if (nav) return go(nav.dataset.screen);
  if (e.target.closest('[data-action="new-chat"]')) return newChat();
  const recent = e.target.closest('[data-recent]');
  if (recent) {
    state.chat = { title: DEMO.recentChats[+recent.dataset.recent], messages: [], loading: false };
    go('chat');
    ask(state.chat.title);
  }
});

$main.addEventListener('submit', (e) => {
  const form = e.target.closest('[data-form="chat"]');
  if (!form) return;
  e.preventDefault();
  ask(form.q.value);
});

$main.addEventListener('input', (e) => {
  if (e.target.id === 'post-search') onSearchInput(e.target.value);
});

$main.addEventListener('click', async (e) => {
  const el = e.target.closest('button');
  if (!el) return;
  const d = el.dataset;

  if (d.ask) return ask(d.ask);
  if (d.sideDigest) { state.digest.side = d.sideDigest; return loadDigest(); }
  if (d.searchSide) { state.search.side = d.searchSide; render(); return loadSearch(); }
  if (d.days) { state.search.days = +d.days; render(); return loadSearch(); }
});

// ---------- Старт ----------
loadMe();
renderRecent();
go('chat');
