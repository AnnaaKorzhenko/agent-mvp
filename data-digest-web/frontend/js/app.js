// Data Digest — фронтенд без збірки (vanilla JS).

const state = {
  screen: 'chat',
  chat: { title: 'Новий чат', messages: [], loading: false },
  digest: { period: 'day', category: 'all', items: [], loading: false },
  search: { q: '', platform: 'all', lang: 'all', items: [], loading: false },
  profile: null,
  profileSaved: false
};

const $main = document.getElementById('main');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON_SEND = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';
const ICON_BOT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h16"/></svg>';
const ICON_SEARCH = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>';

const CAT_LABEL = { tech: 'Оборонні технології', front: 'Інновації на фронті', invest: 'Оборонні інвестиції' };
const REL = { high: ['висока', 'high'], medium: ['середня', ''], low: ['низька', 'low'] };

// ---------- Навігація ----------
function go(screen) {
  state.screen = screen;
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.screen === screen));
  render();
  if (screen === 'digests') loadDigest();
  if (screen === 'search') loadSearch();
  if (screen === 'profile' && !state.profile) loadProfile();
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
function renderDigests() {
  const d = state.digest;
  const cats = DEMO.categories.map((c) =>
    `<button class="chip ${d.category === c.id ? 'on' : ''}" data-cat="${c.id}">${esc(c.label)}</button>`).join('');
  const list = d.loading ? '<p class="sub">Завантаження…</p>'
    : d.items.length ? d.items.map((s) => {
        const [relText, relCls] = REL[s.relevance] || [s.relevance, ''];
        return `
        <article class="story">
          <div class="row">
            <span class="tag accent">${esc(CAT_LABEL[s.category] || s.category)}</span>
            <span class="tag">${esc(s.side)}</span>
            <span class="tag ${relCls}">Актуальність: ${esc(relText)}</span>
          </div>
          <h2>${esc(s.title)}</h2>
          <p>${esc(s.summary)}</p>
          <div class="story-foot">
            <span>${esc(s.posts_count)} постів у сюжеті</span>
            <button class="btn-outline" data-ask="${esc('Розкажи детальніше про сюжет: ' + s.title)}">Запитати аналітика</button>
          </div>
        </article>`;
      }).join('')
    : '<p class="sub">За цей період сюжетів немає.</p>';

  return `
    <div class="page"><div class="page-inner">
      <div class="head-row">
        <div>
          <h1 class="h1">Дайджест</h1>
          <span class="sub">${d.period === 'day' ? 'Сьогодні' : 'Останні 7 днів'} · сюжети згруповано з постів Telegram, X та сайтів</span>
        </div>
        <div class="segmented" role="group" aria-label="Період">
          <button class="${d.period === 'day' ? 'on' : ''}" data-period="day">Сьогодні</button>
          <button class="${d.period === 'week' ? 'on' : ''}" data-period="week">Тиждень</button>
        </div>
      </div>
      <div class="row">${cats}</div>
      <div class="stories">${list}</div>
    </div></div>`;
}

async function loadDigest() {
  state.digest.loading = true;
  if (state.screen === 'digests') render();
  try { state.digest.items = await api.getDigest(state.digest.period, state.digest.category); }
  catch (e) { state.digest.items = []; console.error(e); }
  state.digest.loading = false;
  if (state.screen === 'digests') render();
}

// ---------- Пошук ----------
function chipsHtml(list, current, attr) {
  return list.map(([id, label]) =>
    `<button class="chip ${current === id ? 'on' : ''}" data-${attr}="${id}">${esc(label)}</button>`).join('');
}

function resultsHtml() {
  const s = state.search;
  if (s.loading) return '<div class="no-results">Пошук…</div>';
  if (!s.items.length) return '<div class="no-results">Нічого не знайдено. Спробуйте інші слова або зніміть фільтри.</div>';
  return s.items.map((r) => `
    <div class="result">
      <div class="result-meta">
        <span class="platform">${esc(r.platform)}</span>
        <span class="src">${esc(r.source)}</span>
        <span class="when">${esc(r.lang)} · ${esc(r.date)}</span>
        <span class="tag">${esc(CAT_LABEL[r.category] || r.category)}</span>
      </div>
      <p>${esc(r.summary)}</p>
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
        <input id="post-search" value="${esc(s.q)}" autocomplete="off" placeholder="Ключові слова, назва каналу, технологія…">
      </div>
      <div class="filters">
        <div class="row"><span class="filter-label">Платформа</span>${chipsHtml([['all', 'Усі'], ['Telegram', 'Telegram'], ['X', 'X'], ['Web', 'Сайти']], s.platform, 'platform')}</div>
        <div class="row"><span class="filter-label">Мова</span>${chipsHtml([['all', 'Усі'], ['UA', 'UA'], ['RU', 'RU'], ['EN', 'EN']], s.lang, 'lang')}</div>
      </div>
      <span class="sub" id="results-count">Знайдено постів: ${s.items.length}</span>
      <div class="results" id="results">${resultsHtml()}</div>
    </div></div>`;
}

async function loadSearch() {
  const s = state.search;
  try { s.items = await api.searchPosts(s.q, s.platform, s.lang); }
  catch (e) { s.items = []; console.error(e); }
  // оновлюємо лише результати, щоб не збивати фокус у полі пошуку
  const box = document.getElementById('results');
  if (box) {
    box.innerHTML = resultsHtml();
    document.getElementById('results-count').textContent = 'Знайдено постів: ' + s.items.length;
  }
}

let searchTimer;
function onSearchInput(value) {
  state.search.q = value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadSearch, 250);
}

// ---------- Профіль ----------
function renderProfile() {
  const p = state.profile;
  if (!p) return '<div class="page"><div class="page-inner narrow"><p class="sub">Завантаження…</p></div></div>';
  const interests = DEMO.interests.map((i) => {
    const on = p.interests.includes(i.id);
    return `<button class="chip ${on ? 'on' : ''}" aria-pressed="${on}" data-interest="${i.id}">${esc(i.label)}</button>`;
  }).join('');
  const schedules = [['daily', 'Щодня о 08:00'], ['weekly', 'Щотижня, понеділок'], ['off', 'Лише на запит']]
    .map(([id, l]) => `<button class="chip ${p.schedule === id ? 'on' : ''}" data-schedule="${id}">${l}</button>`).join('');
  const sides = [['UA', 'Українські джерела'], ['RU', 'Російські джерела'], ['Світ', 'Міжнародні джерела']]
    .map(([id, l]) => {
      const on = p.sides.includes(id);
      return `<button class="chip ${on ? 'on' : ''}" aria-pressed="${on}" data-side="${id}">${l}</button>`;
    }).join('');

  return `
    <div class="page"><div class="page-inner narrow">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 class="h1">Профіль та інтереси</h1>
        <span class="sub">Від цього залежить, що потрапляє у ваш персональний дайджест і як відповідає аналітик.</span>
      </div>
      <section class="card">
        <h2>Теми інтересу</h2>
        <div class="row">${interests}</div>
        <span class="sub">Вибрано: ${p.interests.length}</span>
      </section>
      <section class="card">
        <h2>Розсилка дайджесту</h2>
        <div class="row">${schedules}</div>
        <span class="sub">Дайджест приходить у Telegram-бот і з'являється тут, у розділі «Дайджести».</span>
      </section>
      <section class="card">
        <h2>Джерела в дайджесті</h2>
        <div class="row">${sides}</div>
      </section>
      <div class="row">
        <button class="btn-primary" data-action="save-profile">Зберегти</button>
        ${state.profileSaved ? '<span class="saved" role="status">Налаштування збережено</span>' : ''}
      </div>
    </div></div>`;
}

async function loadProfile() {
  try { state.profile = await api.getProfile(); }
  catch (e) { console.error(e); state.profile = { interests: [], schedule: 'daily', sides: [] }; }
  if (state.screen === 'profile') render();
}

const toggle = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : arr.concat([v]));

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
  if (d.period) { state.digest.period = d.period; return loadDigest(); }
  if (d.cat) { state.digest.category = d.cat; return loadDigest(); }
  if (d.platform) { state.search.platform = d.platform; render(); return loadSearch(); }
  if (d.lang) { state.search.lang = d.lang; render(); return loadSearch(); }
  if (d.interest) { state.profile.interests = toggle(state.profile.interests, d.interest); state.profileSaved = false; return render(); }
  if (d.side) { state.profile.sides = toggle(state.profile.sides, d.side); state.profileSaved = false; return render(); }
  if (d.schedule) { state.profile.schedule = d.schedule; state.profileSaved = false; return render(); }
  if (d.action === 'save-profile') {
    await api.saveProfile(state.profile);
    state.profileSaved = true;
    render();
  }
});

// ---------- Старт ----------
renderRecent();
go('chat');
