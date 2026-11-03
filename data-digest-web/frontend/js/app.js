// Data Digest — фронтенд без збірки (vanilla JS).

const state = {
  screen: 'chat',
  chat: { id: null, title: 'Новий чат', messages: [], loading: false },
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

// ---------- Нещодавні чати ----------
let recentChats = [];

function renderRecent() {
  const box = document.getElementById('recent-list');
  box.innerHTML = recentChats.length
    ? recentChats.map((c) =>
      `<button class="recent-item ${c.id === state.chat.id ? 'on' : ''}" data-recent="${esc(c.id)}" title="${esc(c.title)}">${esc(c.title)}</button>`).join('')
    : '<span class="sub recent-empty">Тут з’являться ваші чати.</span>';
}

async function loadRecent() {
  try { recentChats = (await api.listChats()).chats || []; } catch (e) { recentChats = []; }
  renderRecent();
}

// ---------- Чат ----------
const TOOL_LABEL = {
  search_posts: 'Шукаю в постах…', get_digest: 'Відкриваю дайджест…', get_morning_digest: 'Відкриваю ранковий дайджест…',
  get_post: 'Читаю пост…', get_profile: 'Дивлюсь ваш профіль…', get_insights: 'Згадую, що знаю про вас…',
  deep_dive: 'Збираю все про це…', save_findings: 'Зберігаю знахідки…', get_analysis: 'Готую аналіз…',
  save_report: 'Зберігаю звіт…', track_topic: 'Додаю тему до відстеження…', set_goal: 'Оновлюю цілі…',
  save_insight: 'Запам’ятовую…', record_feedback: 'Записую відгук…', get_proposals: 'Дивлюсь пропозиції…',
  answer_proposal: 'Виконую…', set_agent_prefs: 'Оновлюю налаштування…', correct_profile: 'Виправляю профіль…'
};
const ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

// Невеликий безпечний markdown: спершу екрануємо все, потім — посилання (лише http/https), жирний, курсив, списки, заголовки.
function inline(s) {
  return s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s.,;:!?)])/g, '$1<i>$2</i>');
}

function md(src) {
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of esc(src).split('\n')) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/)) || (m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      const kind = /^\s*\d/.test(line) ? 'ol' : 'ul';
      if (list !== kind) { close(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
      close(); out.push(`<h3>${inline(m[1])}</h3>`);
    } else if (!line.trim()) {
      close();
    } else {
      close(); out.push(`<p>${inline(line)}</p>`);
    }
  }
  close();
  return out.join('');
}

function composer(big) {
  const busy = state.chat.loading;
  const button = busy
    ? `<button class="send stop" type="button" data-action="stop" aria-label="Зупинити">${ICON_STOP}</button>`
    : `<button class="send" type="submit" aria-label="Надіслати">${ICON_SEND}</button>`;
  if (big) {
    return `
      <form class="composer big" data-form="chat">
        <label for="composer" class="sr-only">Повідомлення аналітику</label>
        <input id="composer" name="q" autocomplete="off" placeholder="Запитайте про новини, сюжети чи тренди…">
        <div class="composer-bar">
          <div class="row"><span class="mini-pill">Пости UA + RU · ваші цілі</span></div>
          ${button}
        </div>
      </form>`;
  }
  return `
    <form class="composer small" data-form="chat">
      <label for="composer" class="sr-only">Повідомлення аналітику</label>
      <input id="composer" name="q" autocomplete="off" placeholder="Уточніть або поставте нове питання…">
      ${button}
    </form>`;
}

function botHtml(m) {
  const status = m.streaming
    ? `<div class="bot-status typing">${esc(m.status || 'Думаю…')}</div>` : '';
  return `
    <div class="msg-bot">
      <div class="bot-avatar" aria-hidden="true">${ICON_BOT}</div>
      <div class="bot-body">
        <div class="bot-text md">${md(m.content)}</div>
        ${status}
        ${m.error ? `<div class="bot-error">${esc(m.error)}</div>` : ''}
      </div>
    </div>`;
}

function renderChat() {
  const { messages, title } = state.chat;
  const header = `
    <header class="chat-header">
      <span class="chat-title">${esc(title)}</span>
      <span class="pill">База: News · оновлюється щодня</span>
    </header>`;

  if (state.chat.opening) return `<div class="chat">${header}<div class="empty"><p class="sub">Відкриваю чат…</p></div></div>`;
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
    : botHtml(m)).join('');

  return `
    <div class="chat">${header}
      <div class="thread-wrap">
        <div class="thread" aria-live="polite">${items}</div>
        <div class="composer-dock">
          ${composer(false)}
          <p class="disclaimer">Відповіді базуються на зібраних постах — перевіряйте джерела.</p>
        </div>
      </div>
    </div>`;
}

// Під час відповіді оновлюємо лише останнє повідомлення, щоб не перемальовувати весь екран на кожен шматок тексту.
function paintLast() {
  const last = state.chat.messages[state.chat.messages.length - 1];
  const el = $main.querySelector('.thread .msg-bot:last-child');
  if (state.screen !== 'chat' || !el || !last || last.role !== 'assistant') return;
  el.outerHTML = botHtml(last);
  window.scrollTo(0, document.body.scrollHeight);
}

async function ask(text) {
  const t = (text || '').trim();
  if (!t || state.chat.loading) return;
  if (state.screen !== 'chat') go('chat');
  const chat = state.chat;
  if (!chat.messages.length) chat.title = t.length > 48 ? t.slice(0, 48) + '…' : t;
  chat.messages.push({ role: 'user', content: t });
  const reply = { role: 'assistant', content: '', streaming: true, status: 'Думаю…' };
  chat.messages.push(reply);
  chat.loading = true;
  chat.controller = new AbortController();
  render();
  try {
    if (!chat.id) chat.id = (await api.newChat()).id;
    await api.sendMessage(chat.id, t, (ev) => {
      if (ev.type === 'text') { reply.content += ev.text; reply.status = 'Пишу…'; }
      else if (ev.type === 'tool') reply.status = TOOL_LABEL[ev.name] || 'Працюю з даними…';
      else if (ev.type === 'error') reply.error = ev.detail;
      if (state.chat === chat) paintLast();
    }, chat.controller.signal);
  } catch (e) {
    if (e.name === 'AbortError') reply.error = reply.content ? 'Зупинено.' : 'Зупинено до відповіді.';
    else reply.error = 'Не вдалося отримати відповідь: ' + e.message;
  } finally {
    reply.streaming = false;
    chat.loading = false;
    chat.controller = null;
    if (state.chat === chat && state.screen === 'chat') render();
    loadRecent();
  }
}

function stopAnswer() {
  if (state.chat.controller) state.chat.controller.abort();
}

function newChat() {
  stopAnswer();
  state.chat = { id: null, title: 'Новий чат', messages: [], loading: false };
  go('chat');
  renderRecent();
}

async function openChat(id) {
  stopAnswer();
  const known = recentChats.find((c) => c.id === id);
  state.chat = { id, title: known ? known.title : 'Чат', messages: [], loading: false, opening: true };
  go('chat');
  renderRecent();
  try {
    const res = await api.getChat(id);
    if (state.chat.id !== id) return;
    state.chat.messages = res.messages.map((m) => ({ role: m.role, content: m.content,
      error: m.role === 'assistant' && !m.content ? 'Відповідь не завершилась.' : '' }));
  } catch (e) {
    if (state.chat.id !== id) return;
    state.chat.messages = [{ role: 'assistant', content: '', error: 'Не вдалося відкрити чат: ' + e.message }];
  }
  state.chat.opening = false;
  if (state.screen === 'chat') render();
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
  if (recent) return openChat(recent.dataset.recent);
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

  if (d.action === 'stop') return stopAnswer();
  if (d.ask) return ask(d.ask);
  if (d.sideDigest) { state.digest.side = d.sideDigest; return loadDigest(); }
  if (d.searchSide) { state.search.side = d.searchSide; render(); return loadSearch(); }
  if (d.days) { state.search.days = +d.days; render(); return loadSearch(); }
});

// ---------- Старт ----------
loadMe();
loadRecent();
go('chat');
