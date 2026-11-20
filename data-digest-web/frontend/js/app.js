const state = {
  screen: 'chat',
  chat: { id: null, title: 'Новий чат', messages: [], loading: false },
  // digest: { side: 'both', data: null, loading: false, error: '' },
  digest: { side: 'both', unseen: false, data: null, loading: false, error: '', jump: null, jumpNote: '' },
  search: { q: '', side: 'all', days: 7, items: [], note: '', error: '', loading: false },
  proposals: { items: [], loading: false, error: '', answered: {}, reasonFor: null },
  profile: null,
  profileNote: '',
  push: { checked: false, key: null, on: false, busy: false, note: '' },
  agents: { days: 7, data: null, error: '', note: '' },
  ratings: {},
  rateError: {},
  fullText: {},
  commentFor: null,
  companies: { q: '', country: '', sector: '', items: [], total: 0, countries: [], sectors: [], loading: false, error: '' },
};

const $main = document.getElementById('main');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON_SEND = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';
const ICON_BOT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h16"/></svg>';
const ICON_SEARCH = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>';



function go(screen) {
  state.screen = screen;
  if (screen === 'profile') state.profileNote = '';
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.screen === screen));
  render();
  if (screen === 'digests') loadDigest();
  if (screen === 'search') loadSearch();
  if (screen === 'profile') { state.push.note = ''; loadProfile(); loadPush(); }
  if (screen === 'proposals') loadProposals();
  if (screen === 'agents') loadAgents();
  if (screen === 'map') loadCompanies();
}

function render() {
  const view = { chat: renderChat, digests: renderDigests, search: renderSearch, profile: renderProfile,
    proposals: renderProposals, agents: renderAgents, map: renderMap }[state.screen];
  $main.innerHTML = view();
  if (state.screen === 'chat') {
    const input = $main.querySelector('.composer input');
    if (input) input.focus();
    const thread = $main.querySelector('.thread');
    if (thread) window.scrollTo(0, document.body.scrollHeight);
  }
}

// ---------- Оцінки (новини й відповіді аналітика) ----------
// key: d:<digest>/<n> | p:<post> | r:<message>; ціль запам'ятовується під час малювання.
const rateTargets = {};

function rateHtml(key, kind, target) {
  rateTargets[key] = target;
  const cur = state.ratings[key];
  const btn = (r, label) =>
    `<button class="rate ${cur === r ? 'on' : ''}" data-rate="${esc(key)}" data-reaction="${r}" aria-pressed="${cur === r}">${label}</button>`;
  const comment = state.commentFor === key
    ? `<form class="comment" data-comment="${esc(key)}">
         <label class="sr-only" for="c-${esc(key)}">Чому не корисно</label>
         <input id="c-${esc(key)}" name="text" maxlength="2000" autocomplete="off" placeholder="Чому? Необов’язково: допоможе підбирати краще">
         <button class="btn-outline" type="submit">Надіслати</button>
       </form>` : '';
  const err = state.rateError[key] ? `<span class="sub">${esc(state.rateError[key])}</span>` : '';
  return `<div class="rate-row" data-rate-row="${esc(key)}" data-kind="${kind}">${btn('useful', 'Корисно')}${btn('not_useful', 'Не корисно')}${err}${comment}</div>`;
}

function repaintRate(key) {
  document.querySelectorAll('[data-rate-row]').forEach((el) => {
    if (el.dataset.rateRow === key) el.outerHTML = rateHtml(key, el.dataset.kind, rateTargets[key]);
  });
  const input = state.commentFor === key && document.querySelector(`form[data-comment] input`);
  if (input) input.focus();
}

async function sendRating(key, reaction, text) {
  const t = rateTargets[key];
  if (!t) return;
  if (t.messageId) return api.rateReply(t.chatId, t.messageId, reaction, text);
  return api.rate(t, text && !reaction ? null : reaction, text);
}

async function onRate(key, reaction) {
  const before = state.ratings[key];
  state.ratings[key] = reaction;
  state.commentFor = reaction === 'not_useful' ? key : null;
  delete state.rateError[key];
  repaintRate(key);
  try { await sendRating(key, reaction); }
  catch (e) {
    state.ratings[key] = before;
    state.commentFor = null;
    state.rateError[key] = 'Не збереглося: ' + e.message;
    repaintRate(key);
  }
}

async function onComment(key, text) {
  state.commentFor = null;
  repaintRate(key);
  if (!text.trim()) return;
  const t = rateTargets[key];
  try { await (t && t.messageId ? sendRating(key, 'not_useful', text) : sendRating(key, null, text)); }
  catch (e) { state.rateError[key] = 'Коментар не зберігся: ' + e.message; repaintRate(key); }
}


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


const TOOL_LABEL = {
  search_posts: 'Шукаю в постах…', get_digest: 'Відкриваю дайджест…', get_morning_digest: 'Відкриваю ранковий дайджест…',
  get_post: 'Читаю пост…', get_profile: 'Дивлюсь ваш профіль…', get_insights: 'Згадую, що знаю про вас…',
  deep_dive: 'Збираю все про це…', web_research: 'Шукаю в інтернеті…', save_findings: 'Зберігаю знахідки…', get_analysis: 'Готую аналіз…',
  save_report: 'Зберігаю звіт…', track_topic: 'Додаю тему до відстеження…', set_goal: 'Оновлюю цілі…',
  save_insight: 'Запам’ятовую…', record_feedback: 'Записую відгук…', get_proposals: 'Дивлюсь пропозиції…',
  answer_proposal: 'Виконую…', set_agent_prefs: 'Оновлюю налаштування…', correct_profile: 'Виправляю профіль…'
};
const ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';


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
        ${!m.streaming && m.id && m.content && state.chat.id
          ? rateHtml('r:' + m.id, 'reply', { chatId: state.chat.id, messageId: m.id }) : ''}
      </div>
    </div>`;
}

function renderChat() {
  const { messages, title } = state.chat;
  const canDelete = state.chat.id && messages.length && !state.chat.loading && !state.chat.opening;
  const del = !canDelete ? ''
    : state.chat.confirmDelete
      ? `<span class="row chat-delete"><span class="sub">Видалити цей чат?</span>
           <button class="btn-text" data-chat-delete="yes">Так, видалити</button>
           <button class="btn-text" data-chat-delete="no">Скасувати</button></span>`
      : '<button class="btn-text" data-chat-delete="ask">Видалити чат</button>';
  const header = `
    <header class="chat-header">
      <span class="chat-title">${esc(title)}</span>
      <span class="row chat-actions">${del}<span class="pill">База: News · оновлюється щодня</span></span>
      ${state.chat.deleteError ? `<span class="sub">${esc(state.chat.deleteError)}</span>` : ''}
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


function paintLast() {
  const last = state.chat.messages[state.chat.messages.length - 1];
  const el = $main.querySelector('.thread .msg-bot:last-child');
  if (state.screen !== 'chat' || !el || !last || last.role !== 'assistant') return;
  el.outerHTML = botHtml(last);
  window.scrollTo(0, document.body.scrollHeight);
}

async function ask(text, item) {
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
      else if (ev.type === 'done') reply.id = ev.message_id;
      if (state.chat === chat) paintLast();
    }, chat.controller.signal, item);
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

async function deleteChat() {
  const chat = state.chat;
  try {
    await api.deleteChat(chat.id);
  } catch (e) {
    chat.confirmDelete = false;
    chat.deleteError = 'Не вдалося видалити чат: ' + e.message;
    return render();
  }
  recentChats = recentChats.filter((c) => c.id !== chat.id);
  if (state.chat === chat) newChat(); else renderRecent();
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
    state.chat.messages = res.messages.map((m) => {
      if (m.rating) state.ratings['r:' + m.id] = m.rating;
      return { id: m.id, role: m.role, content: m.content,
        error: m.role === 'assistant' && !m.content ? 'Відповідь не завершилась.' : '' };
    });
  } catch (e) {
    if (state.chat.id !== id) return;
    state.chat.messages = [{ role: 'assistant', content: '', error: 'Не вдалося відкрити чат: ' + e.message }];
  }
  state.chat.opening = false;
  if (state.screen === 'chat') render();
}

const FULL_TEXT_MAX = 3;
const RU_CLAIM_NOTE = 'Заява російського каналу, не перевірений факт.';

function fullTextHtml(ids) {
  const key = (ids || []).slice(0, FULL_TEXT_MAX).join(',');
  if (!key) return '';
  const f = state.fullText[key] || {};
  let body = '';
  if (f.open) {
    if (f.loading) body = '<p class="sub">Завантаження…</p>';
    else if (f.error) body = `<p class="sub">${esc(f.error)}</p>`;
    else {
      const texts = (f.posts || []).filter((p) => p.full_text || p.full_text_note);
      body = texts.length ? texts.map((p) => p.full_text
        ? `<p class="sub">${esc(RU_CLAIM_NOTE)}${p.channel?.username ? ' @' + esc(p.channel.username) : ''}</p><p class="full-text">${esc(p.full_text)}</p>`
        : `<p class="sub">${esc(p.full_text_note)}</p>`).join('')
        : '<p class="sub">Повного тексту немає.</p>';
    }
  }
  return `<div class="full-text-box" data-full-row="${esc(key)}">
      <div class="row"><button class="btn-text" data-full-text="${esc(key)}" aria-expanded="${!!f.open}">${f.open ? 'Згорнути текст' : 'Повний текст'}</button></div>
      ${body}
    </div>`;
}

function repaintFullText(key) {
  document.querySelectorAll('[data-full-row]').forEach((el) => {
    if (el.dataset.fullRow === key) el.outerHTML = fullTextHtml(key.split(','));
  });
}

async function toggleFullText(key) {
  const f = state.fullText[key] || (state.fullText[key] = {});
  f.open = !f.open;
  if (f.open && !f.posts && !f.loading) {
    f.loading = true; f.error = '';
    repaintFullText(key);
    try { f.posts = await Promise.all(key.split(',').map((id) => api.getPostText(id))); }
    catch (e) { f.error = 'Не вдалося завантажити текст: ' + e.message; }
    f.loading = false;
  }
  repaintFullText(key);
}

const SIDES = [['both', 'Усе'], ['ua', 'Українські джерела'], ['ru', 'Російські джерела'], ['world', 'Світові джерела'], ['video', 'Відео'], ['topics', 'Ваші теми й інтереси']];
const SIDE_LABEL = { ua: 'Українські джерела', ru: 'Російські джерела', world: 'Світові джерела', video: 'Відео, які можуть зацікавити', topics: 'Ваші теми й інтереси' };
const SIDE_NOTE = { ru: 'Заяви російських каналів, а не перевірені факти.', video: 'Короткий зміст кожного відео; відео російських каналів — їхні заяви, а не факти.' };

const noEmoji = (s) => String(s || '').replace(/[\p{Extended_Pictographic}\p{Regional_Indicator}\u{FE0F}\u{200D}]/gu, '').trim();

const INVEST_RE = /invest|інвест|фінанс|funding/i;
const isInvest = (s) => INVEST_RE.test(s.category || '') || INVEST_RE.test(s.title || '');

function host(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch (e) { return 'джерело'; }
}

const isVideo = (u) => /^(m\.|music\.)?(youtube\.com|youtu\.be)$/.test(host(u));

function videoId(u) {
  try {
    const url = new URL(u);
    if (!isVideo(u)) return '';
    const id = url.hostname.endsWith('youtu.be') ? url.pathname.slice(1)
      : url.searchParams.get('v') || (url.pathname.match(/^\/(?:shorts|embed|live)\/([^/]+)/) || [])[1] || '';
    return /^[\w-]{6,20}$/.test(id) ? id : '';
  } catch (e) { return ''; }
}

function durationText(sec) {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = String(sec % 60).padStart(2, '0');
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${s}` : `${m}:${s}`;
}


function videoHeadHtml(it) {
  const url = (it.source_urls || []).find(isVideo) || '';
  const id = videoId(url);
  const meta = [it.channel, id ? '' : durationText(it.duration_sec)].filter(Boolean).map(esc).join(' · ');   // тривалість уже на обкладинці
  const thumb = id ? `<a class="video-thumb" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="Дивитися на YouTube">
        <img src="https://i.ytimg.com/vi/${esc(id)}/mqdefault.jpg" alt="" loading="lazy" referrerpolicy="no-referrer">
        ${it.duration_sec ? `<span class="video-time">${esc(durationText(it.duration_sec))}</span>` : ''}
      </a>` : '';
  return `${thumb}
      <div class="video-head">
        ${it.title ? `<h2>${esc(it.title)}</h2>` : ''}
        ${meta ? `<span class="sub">${meta}</span>` : ''}
      </div>`;
}

const isRutube = (u) => /(^|\.)rutube\.ru$/.test(host(u));

function sourcesHtml(urls) {
  return (urls || []).slice(0, 6).map((u) =>
    `<a class="source" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${isVideo(u) ? '▶ YouTube' : isRutube(u) ? '▶ Rutube' : esc(host(u))}</a>`).join('');
}

function whyHtml(why) {
  return (why || []).slice(0, 2).map((w) => `<span class="tag accent">${esc(noEmoji(w))}</span>`).join('');
}

function itemHtml(it, side, digestId) {

  const video = side === 'video' || !!it.title;
  const about = ((it.title ? it.title + ': ' : '') + it.text).slice(0, 160);
  const item = digestId ? esc(JSON.stringify({ digest_id: digestId, item_n: it.n })) : '';
  return `
    <article class="story">
      <div class="row">
        <span class="tag">#${esc(it.ref)}</span>
        ${it.is_russian_source_claim ? '<span class="tag high">Заява російських каналів</span>' : ''}
        ${whyHtml(it.why)}
      </div>
      ${video ? `<div class="video">${videoHeadHtml(it)}</div>` : ''}
      <p>${esc(it.text)}</p>
      <div class="story-foot">
        <div class="row">${sourcesHtml(it.source_urls)}</div>
        <div class="row">
          <button class="btn-outline" data-ask="${esc('Поясни #' + it.ref + ' з бази: ' + about)}" data-item="${item}">Пояснити</button>
          <button class="btn-text" data-ask="${esc('Глибше про #' + it.ref + ': ' + about)}" data-item="${item}">Глибше, з інтернетом</button>
        </div>
      </div>
      ${!video && (side === 'ru' || it.is_russian_source_claim) ? fullTextHtml(it.source_post_ids) : ''}
      ${digestId ? rateHtml(`d:${digestId}/${it.n}`, 'news', { target_type: 'digest_item', digest_id: digestId, item_n: it.n }) : ''}
    </article>`;
}

function digestHtml(data) {
  if (!data) return '';
  const blocks = (data.digests || []).map((d) => `
    <section class="stories">
      <h2 class="h2">${esc(SIDE_LABEL[d.side] || d.side)}</h2>
      ${SIDE_NOTE[d.side] ? `<span class="sub">${esc(SIDE_NOTE[d.side])}</span>` : ''}
      ${d.sections.map((s) => `
                ${s.title || isInvest(s) ? `<div class="section-label"${isInvest(s) ? ' data-invest="1"' : ''}>${esc(noEmoji(s.title))}</div>` : ''}
        ${s.items.map((it) => itemHtml(it, d.side, d.digest_id)).join('')}`).join('')}
    </section>`).join('');
    const notes = [state.digest.jumpNote, data.note, (data.missing_sides || []).length ? 'Ще немає: ' + data.missing_sides.map((m) => SIDE_LABEL[m] || m).join(', ') : '']
    .filter(Boolean).map((n) => `<p class="sub">${esc(n)}</p>`).join('');
  const n = (data.proposals || []).length;
  const offer = n ? `<button class="notice" data-go="proposals">Система має для вас пропозицій: ${n}. Переглянути</button>` : '';
  if (data.proposals) setBadge(n);
  return offer + notes + (blocks || '<p class="sub">Дайджесту за цей день немає.</p>');
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
      <div class="row">
        <div class="segmented" role="group" aria-label="Джерела">${seg}</div>
        <button class="chip ${d.unseen ? 'on' : ''}" role="switch" aria-checked="${d.unseen}" data-unseen-digest="${d.unseen ? '0' : '1'}">Лише нове за сьогодні</button>
      </div>
      ${d.loading ? '<p class="sub">Завантаження…</p>' : d.error ? `<p class="sub">${esc(d.error)}</p>` : digestHtml(d.data)}
    </div></div>`;
}

async function loadDigest() {
  state.digest.loading = true;
  state.digest.error = '';
  if (state.screen === 'digests') render();
  try {
    state.digest.data = await api.getDigest(state.digest.side, null, state.digest.unseen);
    (state.digest.data.digests || []).forEach((d) => (d.sections || []).forEach((sec) => (sec.items || []).forEach((it) => {
      if (it.my_reaction) state.ratings[`d:${d.digest_id}/${it.n}`] = it.my_reaction;
    })));
  }
  catch (e) { state.digest.data = null; state.digest.error = 'Не вдалося завантажити дайджест: ' + e.message; }
  state.digest.loading = false;
  if (state.screen === 'digests') render();
  if (state.digest.jump === 'invest' && state.screen === 'digests') {
    state.digest.jump = null;
    const target = $main.querySelector('[data-invest]');
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    else { state.digest.jumpNote = 'У дайджесті за цей день розділу про інвестиції немає.'; render(); state.digest.jumpNote = ''; }
  }
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
      ${r.source_side === 'ru' && r.post_id ? fullTextHtml([r.post_id]) : ''}
      ${r.post_id ? rateHtml('p:' + r.post_id, 'news', { target_type: 'post', post_id: r.post_id }) : ''}
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
    try {
      const r = await api.searchPosts(s.q, s.side, s.days);
      s.items = r.results || []; s.note = r.note || '';
      s.items.forEach((p) => { if (p.my_reaction) state.ratings['p:' + p.post_id] = p.my_reaction; });
    }
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


const KIND_LABEL = { goal: 'Ціль', topic: 'Тема', advice: 'Порада' };

function setBadge(n) {
  const b = document.getElementById('proposals-badge');
  if (!b) return;
  b.hidden = !n;
  b.textContent = n ? String(n) : '';
}

function proposalHtml(p) {
  const pr = state.proposals;
  const done = pr.answered[p.proposal_id];
  let actions;
  if (done) actions = `<p class="sub done">${esc(done)}</p>`;
  else if (pr.reasonFor === p.proposal_id) {
    actions = `
      <form class="reason" data-reason="${esc(p.proposal_id)}">
        <label class="sr-only" for="r-${esc(p.proposal_id)}">Чому ні</label>
        <input id="r-${esc(p.proposal_id)}" name="reason" maxlength="500" autocomplete="off" placeholder="Чому ні? Необов’язково: система не пропонуватиме схоже">
        <div class="row">
          <button class="btn-outline" type="submit">Відхилити</button>
          <button class="btn-text" type="button" data-cancel-reason="1">Скасувати</button>
        </div>
      </form>`;
  } else {
    actions = `
      <div class="row">
        <button class="btn-primary" data-yes="${esc(p.proposal_id)}">Так, ${esc(p.yes || 'прийняти')}</button>
        <button class="btn-outline" data-no="${esc(p.proposal_id)}">Ні</button>
      </div>`;
  }
  return `
    <article class="story proposal">
      <div class="row"><span class="tag accent">${esc(KIND_LABEL[p.kind] || p.kind)}</span></div>
      <p><b>${esc(p.title)}</b></p>
      ${p.why ? `<p class="sub">${esc(p.why)}</p>` : ''}
      ${(p.links || []).length ? `<div class="row">${sourcesHtml(p.links)}</div>` : ''}
      ${actions}
    </article>`;
}

function renderProposals() {
  const pr = state.proposals;
  const body = pr.loading ? '<p class="sub">Завантаження…</p>'
    : pr.error ? `<p class="sub">${esc(pr.error)}</p>`
    : pr.items.length ? pr.items.map(proposalHtml).join('')
    : '<p class="sub">Нових пропозицій немає. Система пропонує цілі, теми й поради, коли помічає щось для вас.</p>';
  return `
    <div class="page"><div class="page-inner narrow">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 class="h1">Пропозиції</h1>
        <span class="sub">Що система помітила для вас. Нічого не змінюється, доки ви не скажете «так». Скільки й яких пропозицій показувати — у профілі.</span>
      </div>
      ${body}
    </div></div>`;
}

async function loadProposals() {
  const pr = state.proposals;
  pr.loading = !pr.items.length;
  pr.error = '';
  if (state.screen === 'proposals') render();
  try {
    pr.items = (await api.getProposals()).proposals || [];
    setBadge(pr.items.filter((p) => !pr.answered[p.proposal_id]).length);
  } catch (e) { pr.error = 'Не вдалося завантажити пропозиції: ' + e.message; }
  pr.loading = false;
  if (state.screen === 'proposals') render();
}

async function answerProposal(id, answer, reason) {
  const pr = state.proposals;
  pr.reasonFor = null;
  pr.answered[id] = answer === 'accept' ? 'Виконую…' : 'Зберігаю…';
  render();
  try {
    const r = await api.answerProposal(id, answer, reason);
    pr.answered[id] = r.accepted ? 'Готово: ' + r.done : 'Відхилено. Система врахує це.';
    setBadge(pr.items.filter((p) => !pr.answered[p.proposal_id]).length);
    if (r.ask) { newChat(); ask(r.ask); return; }      // порада, чиє «так» — запит до аналітика
  } catch (e) { delete pr.answered[id]; pr.error = 'Не вдалося: ' + e.message; }
  if (state.screen === 'proposals') render();
}


const ABOUT_LABEL = {
  summary: 'Коротко про вас', interest: 'Інтереси', intent: 'Над чим ви зараз працюєте', preference: 'Як ви хочете отримувати новини',
  goal: 'Цілі, які ви згадували', tracked: 'Що ви відстежуєте', known: 'Що ви вже знаєте', avoid: 'Чого уникати',
  fact: 'Про вашу роботу', feedback: 'Ваші відгуки'
};
const CORRECTABLE = new Set(['interest', 'intent']);   // те, що система вивела сама: можна сказати «це не про мене»
const TOPIC_STATUS = { active: 'відстежується', explored: 'досліджено', expired: 'завершено', stopped: 'зупинено' };
const PREF_KINDS = [['goal', 'Цілі'], ['topic', 'Теми'], ['advice', 'Поради']];

function listHtml(items, empty) {
  return items.length ? `<ul class="plain">${items.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>` : `<span class="sub">${esc(empty)}</span>`;
}

function goalsHtml(goals) {
  return `
    ${goals.length ? `<ul class="plain editable">${goals.map((g) => `
      <li><span>${esc(g)}</span><button class="btn-text" data-goal-remove="${esc(g)}">Прибрати</button></li>`).join('')}</ul>`
      : '<span class="sub">Цілей ще немає.</span>'}
    <form class="inline-form" data-form="goal">
      <label for="goal-new" class="sr-only">Нова ціль</label>
      <input id="goal-new" name="text" maxlength="300" autocomplete="off" placeholder="Нова ціль, наприклад: стежити за розвитком НРК">
      <button class="btn-outline" type="submit">Додати</button>
    </form>`;
}

function topicsHtml(topics) {
  if (!topics.length) return '<span class="sub">Ви ще нічого не відстежуєте. Тему можна додати з пропозицій або попросити аналітика.</span>';
  return `<ul class="plain editable">${topics.map((t) => {
    const until = t.expires_at && t.status === 'active' ? ' до ' + esc(new Date(t.expires_at).toLocaleDateString('uk-UA')) : '';
    const btn = t.status === 'active' ? `<button class="btn-text" data-topic="${esc(t.name)}" data-topic-action="stop">Припинити</button>`
      : t.status === 'expired' ? `<button class="btn-text" data-topic="${esc(t.name)}" data-topic-action="follow">Продовжити на 30 днів</button>` : '';
    return `<li><span><b>${esc(t.name)}</b> <span class="sub">· ${esc(TOPIC_STATUS[t.status] || t.status)}${until}</span></span>${btn}</li>`;
  }).join('')}</ul>`;
}

function aboutHtml(kind, texts) {
  if (!CORRECTABLE.has(kind)) return listHtml(texts, '');
  return `<ul class="plain editable">${texts.map((t) => `
    <li><span>${esc(t)}</span><button class="btn-text" data-not-me="${esc(t.slice(0, 120))}">Це не про мене</button></li>`).join('')}</ul>`;
}

function prefsHtml(pr) {
  const cards = [0, 1, 2, 3].map((n) =>
    `<button class="${pr.cards === n ? 'on' : ''}" data-prefs-cards="${n}">${n === 0 ? 'Жодної' : n}</button>`).join('');
  const kinds = PREF_KINDS.map(([k, label]) => {
    const on = !pr.off.includes(k);
    return `<button class="chip ${on ? 'on' : ''}" data-prefs-kind="${k}" aria-pressed="${on}">${label}</button>`;
  }).join('');
  return `
    <div class="pref"><span class="filter-label">Пропозицій у дайджесті</span><div class="segmented small" role="group" aria-label="Скільки пропозицій">${cards}</div></div>
    <div class="pref"><span class="filter-label">Що пропонувати</span><div class="row">${kinds}</div></div>
    <div class="pref"><span class="filter-label">У розмові з аналітиком</span>
      <button class="chip ${pr.mentions ? 'on' : ''}" data-prefs-mentions="${pr.mentions ? '0' : '1'}" aria-pressed="${pr.mentions}">${pr.mentions ? 'Згадувати пропозиції' : 'Не згадувати'}</button></div>`;
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
        <span class="sub">Від цього залежить, що потрапляє у «Для вас» і як відповідає аналітик.</span>
      </div>
      ${state.profileNote ? `<p class="notice static" role="status">${esc(state.profileNote)}</p>` : ''}
      <section class="card">
        <h2>Цілі</h2>
        ${goalsHtml(p.goals || [])}
      </section>
      <section class="card">
        <h2>Теми, які ви відстежуєте</h2>
        ${topicsHtml(topics)}
      </section>
      ${about.map(([kind, texts]) => `
        <section class="card">
          <h2>${esc(ABOUT_LABEL[kind] || kind)}</h2>
          ${aboutHtml(kind, texts)}
        </section>`).join('')}
      ${(p.top_tags || []).length ? `
      <section class="card">
        <h2>Що ви читаєте найбільше</h2>
        <div class="row">${p.top_tags.slice(0, 12).map((t) => `<span class="tag">${esc(t.tag)}</span>`).join('')}</div>
      </section>` : ''}
      ${p.agent_prefs ? `
      <section class="card">
        <h2>Пропозиції системи</h2>
        ${prefsHtml(p.agent_prefs)}
      </section>` : ''}
      ${pushHtml()}
    </div></div>`;
}


const PUSH_SUPPORTED = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

function pushHtml() {
  const s = state.push;
  let body;
  if (!PUSH_SUPPORTED) body = '<p class="sub">Цей браузер не підтримує сповіщення. На iPhone додайте сайт на початковий екран («Поділитися» → «На початковий екран») і відкрийте його звідти.</p>';
  else if (!s.checked) body = '<p class="sub">Перевіряю…</p>';
  else if (!s.key) body = '<p class="sub">Сповіщення поки не налаштовані на сервері.</p>';
  else {
    body = `
      <div class="pref"><span class="filter-label">Сповіщення про ранковий дайджест</span>
        <button class="chip ${s.on ? 'on' : ''}" role="switch" aria-checked="${s.on}" data-push="${s.on ? '0' : '1'}" ${s.busy ? 'disabled' : ''}>${s.on ? 'Увімкнено' : 'Вимкнено'}</button></div>
      <p class="sub">Коли дайджест готовий, браузер покаже сповіщення; натисніть його, щоб відкрити «Дайджести».</p>`;
  }
  return `
      <section class="card">
        <h2>Сповіщення</h2>
        ${body}
        ${s.note ? `<p class="notice static" role="status">${esc(s.note)}</p>` : ''}
      </section>`;
}

function vapidBytes(key) {          // base64url -> Uint8Array, як хоче pushManager.subscribe
  const b64 = (key + '='.repeat((4 - key.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

async function loadPush() {
  if (!PUSH_SUPPORTED) return;
  try {
    const [{ key }, reg] = await Promise.all([api.pushKey(), navigator.serviceWorker.register('/sw.js')]);
    const sub = await reg.pushManager.getSubscription();
    Object.assign(state.push, { key, on: !!sub && Notification.permission === 'granted' });
  } catch (e) { state.push.note = 'Не вдалося перевірити сповіщення: ' + e.message; }
  state.push.checked = true;
  if (state.screen === 'profile') render();
}

async function setPush(on) {
  const s = state.push;
  s.busy = true; s.note = ''; render();
  try {
    const reg = await navigator.serviceWorker.register('/sw.js');
    let sub = await reg.pushManager.getSubscription();
    if (on) {
      if (await Notification.requestPermission() !== 'granted') {
        throw new Error('браузер не дав дозволу. Дозвольте сповіщення для цього сайту в налаштуваннях браузера.');
      }
      // ключ сервера змінився — стара підписка вже не прийме push
      if (sub && sub.options.applicationServerKey) {
        const old = new Uint8Array(sub.options.applicationServerKey).join();
        if (old !== vapidBytes(s.key).join()) { await sub.unsubscribe(); sub = null; }
      }
      sub = sub || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: vapidBytes(s.key) });
      await api.pushSubscribe(sub.toJSON());
      s.note = 'Сповіщення увімкнено в цьому браузері.';
    } else {
      if (sub) {
        await api.pushUnsubscribe(sub.endpoint);
        await sub.unsubscribe();
      }
      s.note = 'Сповіщення вимкнено.';
    }
    s.on = on;
  } catch (e) { s.note = 'Не вдалося: ' + e.message; }
  s.busy = false;
  if (state.screen === 'profile') render();
}

async function loadProfile() {
  try { state.profile = await api.getProfile(); }
  catch (e) { state.profile = { error: 'Не вдалося завантажити профіль: ' + e.message }; }
  if (state.screen === 'profile') render();
}


async function profileAction(run, note) {
  state.profileNote = 'Зберігаю…';
  render();
  try { const r = await run(); state.profileNote = typeof note === 'function' ? note(r) : note; }
  catch (e) { state.profileNote = 'Не вдалося: ' + e.message; }
  await loadProfile();
}

function correctedNote(r) {
  const parts = [`Зрозуміло: «${r.not_interested}» вас не цікавить. Такі новини будуть нижче.`];
  if ((r.followed_matching || []).length) parts.push(`Ви відстежуєте схожі теми (${r.followed_matching.join(', ')}): якщо й вони не потрібні, натисніть «Припинити» біля них.`);
  return parts.join(' ');
}



const MAP_SRC = 'https://defense-valley.news/embed/map/';
let mapOpen = true;
try { mapOpen = localStorage.getItem('mapOpen') !== '0'; } catch (e) {  }

const MM_SECTORS = {
  'space-isr': ['Космос і розвідка', 'Space & ISR', 1], 'autonomy-ai': ['ШІ та автономія', 'Autonomy & AI', 2],
  'counter-drone': ['Протидія дронам', 'Counter-drone', 3], 'maritime': ['Морські системи', 'Maritime', 4],
  'drones': ['Дрони', 'Drones', 5], 'electronic-warfare': ['РЕБ', 'Electronic warfare', 6],
  'cyber': ['Кібербезпека', 'Cyber', 7], 'loitering-munitions': ['Баражуючі боєприпаси', 'Loitering munitions', 8],
  'hypersonics-propulsion': ['Гіперзвук і рушії', 'Hypersonics & propulsion', 9], 'materials': ['Матеріали', 'Advanced materials', 10],
  'ground-robotics': ['Наземні роботи (НРК)', 'Ground robotics', 11], 'training-simulation': ['Навчання й симуляція', 'Training & simulation', 12],
  'c4isr': ['Командування, зв’язок, розвідка', 'C4ISR', 13], 'weapons': ['Озброєння й системи захисту', 'Weapons & defence systems', 14],
  'quantum': ['Квантові технології', 'Quantum', 15], 'energy': ['Енергетика й інфраструктура', 'Energy & infrastructure', 16],
  'semiconductors': ['Напівпровідники', 'Semiconductors', 17], 'robotics': ['Робототехніка', 'Robotics', 18],
  'humanoids': ['Гуманоїдні роботи', 'Humanoids', 2], 'robot-ai': ['ШІ для роботів', 'Robot intelligence', 15],
  'cobots': ['Колаборативні роботи', 'Collaborative robotics', 4], 'legged': ['Чотириногі роботи', 'Legged robots', 11],
  'robot-training': ['Навчання й симуляція роботів', 'Robot training', 12], 'soft-robotics': ['М’яка робототехніка', 'Soft robotics', 8],
  'prosthetics': ['Біонічні протези', 'Prosthetics', 3], 'robot-hardware': ['Сенсори й компоненти роботів', 'Robot hardware', 13],
  'exoskeletons': ['Екзоскелети', 'Exoskeletons', 6], 'construction': ['Будівельні роботи', 'Construction robotics', 10],
};
const MM_STAGES = [['preseed', 'Pre-seed'], ['seed', 'Seed'], ['a', 'Series A'], ['b', 'Series B'], ['c', 'Series C+ / growth'],
  ['grant', 'Гранти, EIC'], ['other', 'Інше: борг, частки'], ['nd', 'Невідомо']];
const MM_USE = { def: 'Оборонна', dual: 'Подвійного призначення', civ: 'Цивільна' };
const MM_USE_SHORT = { dual: 'подв.', civ: 'цив.' };
const MM_MAPS = { def: 'Оборонтех', rob: 'Нова робототехніка' };
const MM_MON = { Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12 };
const MM_MON_UA = ['січ', 'лют', 'бер', 'квіт', 'трав', 'черв', 'лип', 'серп', 'вер', 'жовт', 'лист', 'груд'];
const MM_RATE = { '€': 1, '$': 0.86, '£': 1.15 };       // лише для сортування за сумою
const MM_PAGE = 60;                                     // стільки дозволяє /api/companies (limit ≤ 60)

// Призначення, якщо в базі його ще немає: за словами в описі (див. скрипт import_dealroom.py — там те саме)
const MM_DEF = /defen[cs]e|military|armed forces|army|navy|naval|battlefield|warfare|munition|weapon|missile|c-uas|counter[- ]?(drone|uas)|intercept|\bisr\b|surveillance|reconnaissance|nato|\bmod\b|\bdod\b|tactical|combat|national security|border|soldier|police|ballistic|jamming|command and control|targeting|loitering/i;
const MM_CIV = /commercial|civil|industrial|enterprise|consumer|offshore|energy|agricultur|logistics|warehouse|retail|medical|health|amputee|construction|food|culinary|entertainment|automotive|home/i;
const MM_STRONG = /intercept|munition|battlefield|armed forces|military vehicle|weapon|missile|c-uas|counter[- ]?(drone|uas)|loitering|electronic warfare|jamming|combat|soldier|ministry of defence|front ?line|strike/i;

function mmAmount(s) {
  const m = /([€$£])\s?([\d.,]+)\s*([KMB])?/.exec(s || '');
  if (!m) return null;
  const v = parseFloat(m[2].replace(/,/g, '')) * ({ K: 1e-3, M: 1, B: 1e3 }[m[3]] ?? 1e-6);
  return v * MM_RATE[m[1]];
}
function mmStageGroup(st) {
  const l = String(st || '').toLowerCase();
  if (!l || l === 'n/d') return 'nd';
  if (l.includes('pre-seed')) return 'preseed';
  if (l.includes('pre-series a') || l.includes('seed')) return 'seed';
  if (l.includes('series a')) return 'a';
  if (l.includes('series b')) return 'b';
  if (/series [c-z]|pre-ipo|growth/.test(l)) return 'c';
  if (l.includes('grant') || l.includes('eic')) return 'grant';
  return 'other';
}
function mmUse(c, text) {
  if (c.use && MM_USE[c.use]) return c.use;
  const dk = MM_DEF.test(text), ck = MM_CIV.test(text);
  if (MM_STRONG.test(text)) return 'def';
  if (c.maps && !c.maps.includes('def')) return dk ? 'dual' : 'civ';
  return dk && !ck ? 'def' : (dk || ck || c.origin?.includes('dealroom')) ? 'dual' : 'def';
}

// Документ з бази → поля для мапи. Розуміє формат europedefencetech і додаткові поля з Dealroom.
function mmNorm(c, countryLabels) {
  const facts = c.facts || {};
  const secName = facts.sector || '';
  const meta = String(c.card_metadata || '');
  const rest = secName && meta.startsWith(secName) ? meta.slice(secName.length).trim() : meta;
  const [stageRaw, dateRaw = ''] = rest.split(' / ');
  const stage = (stageRaw || String(c.stage || '').split(' / ')[0] || '').trim();   // сервер дає stage разом з датою
  const summary = String((c.funding?.summary || [])[0] || '');
  const fm = /Latest round (.*?) Total raised (.*)$/.exec(summary);
  const latest = fm ? (/^[~≥>]?[€$£][\d.,]+\s*[KMB]?/.exec(fm[1]) || [''])[0] : '';
  const total = (fm ? (/^[~≥>]?[€$£][\d.,\-–]+\s*[KMB]?/.exec(fm[2]) || [''])[0] : '') || c.dealroom_funding || '';
  const why = (c.funding?.details || []).filter((d) => /^why/i.test(d.heading || '')).flatMap((d) => d.content || []).join(' ');
  const dm = /(?:\d{1,2} )?([A-Z][a-z]{2}) (\d{4})/.exec(dateRaw);
  const date = dm ? new Date(+dm[2], MM_MON[dm[1]] - 1, 15) : null;
  const text = [c.description, why].join(' ');
  const words = String(c.name || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/);
  return {
    raw: c, name: c.name || '?', id: c.slug || c.name, desc: c.description || '', loc: c.location || facts.hq || '',
    country: c.country || '', countryLabel: countryLabels[c.country] || String(c.country || '').replace(/-/g, ' '),
    sector: c.sector || 'other', sectorLabel: (MM_SECTORS[c.sector] || [c.sector_label || c.sector || 'Інше'])[0],
    stage: stage || 'n/d', sg: mmStageGroup(stage), dateText: dateRaw, date,
    when: date ? `${MM_MON_UA[date.getMonth()]} ${date.getFullYear()}` : '',
    fresh: date ? (Date.now() - date) / 864e5 <= 183 : false,
    latest, latestEur: mmAmount(latest), total, totalEur: mmAmount(total),
    founded: facts.founded || '', investors: facts.investors || '', website: c.website || facts.website || '',
    why, conf: c.verification?.confidence || '', sources: c.sources || [], profile: c.profile_url || '',
    hot: !!c.hot, signal: c.signal ?? c.dealroom_signal ?? null, drSegment: c.dealroom_segment || '',
    maps: c.maps || ['def'], use: mmUse(c, text),
    mg: (words.length > 1 ? words[0][0] + words[1][0] : String(c.name || '?').slice(0, 2)).toUpperCase(),
    hay: [c.name, c.description, facts.investors, c.location, why].join(' ').toLowerCase(),
  };
}

const mm = { all: [], loaded: false, view: 'map', sort: 'recent', map: '', use: '', hot: false, fresh: false,
  stages: new Set(), open: null };
try { const v = localStorage.getItem('mmView'); if (v === 'map' || v === 'table') mm.view = v; } catch (e) { /* не критично */ }

function renderMap() {
  return `
    <div class="page"><div class="page-inner wide">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 class="h1">Інвестиції в мілтех</h1>
        <span class="sub">Карта європейського фінансування оборонних стартапів і мапа компаній за сегментами.</span>
      </div>

      <details class="card fold" id="map-fold" ${mapOpen ? 'open' : ''}>
        <summary>
          <span class="fold-title">Карта інвестицій у Європі</span>
          <span class="sub">Defense Valley News</span>
        </summary>
        <div class="embed-wrap">
          <iframe class="embed-frame"
            src="${MAP_SRC}"
            title="European defense-tech investment map — Defense Valley News"
            loading="lazy"
            referrerpolicy="strict-origin-when-cross-origin"
            sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe>
        </div>
        <p class="note">Дані: <a href="https://defense-valley.news" target="_blank" rel="noopener noreferrer">Defense Valley News — European defense-tech funding tracker</a></p>
      </details>
      <button class="notice" data-invest-digest="1">Новини про інвестиції з нашої бази: перейти до розділу «Інвестиції» в дайджесті →</button>
      ${companiesSectionHtml()}
    </div></div>`;
}

function mmFiltered() {
  const s = state.companies, q = s.q.trim().toLowerCase();
  return mm.all.filter((c) => (!q || c.hay.includes(q)) && (!s.country || c.country === s.country)
    && (!s.sector || c.sector === s.sector) && (!mm.map || c.maps.includes(mm.map)) && (!mm.use || c.use === mm.use)
    && (!mm.hot || c.hot) && (!mm.fresh || c.fresh) && (!mm.stages.size || mm.stages.has(c.sg)));
}
function mmSorter(a, b) {
  const by = { name: 0, amount: 'latestEur', total: 'totalEur', signal: 'signal' }[mm.sort];
  if (by === 0) return a.name.localeCompare(b.name);
  if (by) return (b[by] ?? -1) - (a[by] ?? -1) || a.name.localeCompare(b.name);
  return (b.date ? +b.date : 0) - (a.date ? +a.date : 0) || a.name.localeCompare(b.name);
}
const mmColor = (sec) => `var(--mm-s${(MM_SECTORS[sec] || [0, 0, 7])[2]})`;
const mmMeta = (c) => [c.sg === 'nd' ? '' : c.stage, c.latest].filter(Boolean).join(' · ') || (c.total ? `усього ${c.total}` : 'раунд невідомий');
const mmHas = (key) => mm.all.some((c) => key(c));

function mmKpisHtml(list) {
  const n = (f) => list.filter(f).length;
  const kpis = [[list.length, 'компаній'], [new Set(list.map((c) => c.country)).size, 'країн'],
    [new Set(list.map((c) => c.sector)).size, 'сегментів']];
  if (mmHas((c) => c.hot)) kpis.push([n((c) => c.hot), '🔥 гарячих']);
  kpis.push([n((c) => c.use === 'def'), 'оборонних'], [n((c) => c.use === 'dual'), 'подвійного призн.'],
    [n((c) => c.use === 'civ'), 'цивільних'], [n((c) => c.fresh), 'раундів за 6 міс.']);
  return kpis.map(([v, l]) => `<div class="mm-kpi"><b>${v}</b><span>${l}</span></div>`).join('');
}
function mmMixHtml(list) {
  const cnt = Object.fromEntries(MM_STAGES.map(([k]) => [k, 0]));
  list.forEach((c) => cnt[c.sg]++);
  const tot = list.length || 1;
  const bar = MM_STAGES.filter(([k]) => cnt[k]).map(([k, l]) =>
    `<i style="width:${cnt[k] / tot * 100}%;background:var(--mm-g-${k})" title="${esc(l)}: ${cnt[k]}"></i>`).join('');
  const legend = MM_STAGES.map(([k, l]) => {
    const on = mm.stages.has(k), off = mm.stages.size && !on;
    return `<button class="mm-leg ${off ? 'off' : ''}" data-mm-stage="${k}" aria-pressed="${on}"><span class="mm-sw" style="background:var(--mm-g-${k})"></span>${esc(l)} <b>${cnt[k]}</b></button>`;
  }).join('');
  return `<div class="mm-bar">${bar}</div><div class="mm-legend">${legend}</div>`;
}
function mmTile(c) {
  return `<button class="mm-tile ${c.use}" data-mm-company="${esc(c.id)}" style="--sc:${mmColor(c.sector)}" title="${esc(c.name)} — ${esc(c.desc)}">
    <span class="mm-mg" aria-hidden="true">${esc(c.mg)}</span>${c.fresh ? '<span class="mm-dot" aria-label="свіжий раунд"></span>' : ''}
    <span class="mm-t"><span class="mm-nm">${MM_USE_SHORT[c.use] ? `<span class="mm-use">${MM_USE_SHORT[c.use]}</span>` : ''}${c.hot ? '🔥 ' : ''}${esc(c.name)}</span>
    <span class="mm-mt">${esc(mmMeta(c))}</span></span></button>`;
}
function mmMapHtml(list) {
  const groups = {};
  list.forEach((c) => (groups[c.sector] ||= []).push(c));
  const order = Object.keys(groups).sort((a, b) => groups[b].length - groups[a].length);
  return `<div class="mm-grid">${order.map((sec) => {
    const [ua, en] = MM_SECTORS[sec] || [groups[sec][0].sectorLabel, ''];
    return `<section class="mm-panel" style="--sc:${mmColor(sec)}">
      <header><h3>${esc(ua)}</h3><span class="mm-en">${esc(en)}</span><span class="mm-n">${groups[sec].length}</span></header>
      <div class="mm-tiles">${groups[sec].sort(mmSorter).map(mmTile).join('')}</div></section>`;
  }).join('')}</div>`;
}
function mmTableHtml(list) {
  const th = (k, l) => `<th><button data-mm-sort="${k}">${l}${mm.sort === k ? ' ↓' : ''}</button></th>`;
  const rows = list.slice().sort(mmSorter).map((c) => `
    <tr tabindex="0" data-mm-company="${esc(c.id)}">
      <td><span class="mm-cname" style="--sc:${mmColor(c.sector)}"><span class="mm-mg" aria-hidden="true">${esc(c.mg)}</span>${c.hot ? '🔥 ' : ''}${esc(c.name)}</span></td>
      <td>${esc(c.sectorLabel)}<br><span class="note">${esc(MM_USE[c.use])}</span></td><td>${esc(c.countryLabel)}</td>
      <td><span class="pill">${esc(c.stage)}</span></td><td class="mm-num">${esc(c.latest || '—')}</td>
      <td class="mm-num">${c.when ? `<span class="pill ${c.fresh ? 'mm-fresh' : ''}">${esc(c.when)}</span>` : '—'}</td>
      <td class="mm-num">${esc(c.total || '—')}</td></tr>`).join('');
  return `<div class="mm-tablebox"><table class="mm-table"><thead><tr>${th('name', 'Компанія')}<th>Сегмент</th><th>Країна</th><th>Стадія</th>
    ${th('amount', 'Останній раунд')}${th('recent', 'Дата')}${th('total', 'Залучено всього')}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function companyFiltersHtml() {
  const s = state.companies;
  const opts = (list, cur, all) => `<option value="">${all}</option>` + list.map((o) =>
    `<option value="${esc(o.id)}" ${o.id === cur ? 'selected' : ''}>${esc(o.label)} (${o.count})</option>`).join('');
  const sel = (id, label, html) => `<label><span class="filter-label">${label}</span><select id="${id}">${html}</select></label>`;
  const own = (map, cur, all, count) => `<option value="">${all}</option>` + Object.entries(map).map(([k, l]) =>
    `<option value="${k}" ${k === cur ? 'selected' : ''}>${esc(l)} (${count(k)})</option>`).join('');
  const sectors = s.sectors.length ? s.sectors : Object.entries(mm.all.reduce((a, c) => ((a[c.sector] = (a[c.sector] || 0) + 1), a), {}))
    .map(([id, count]) => ({ id, label: id, count }));
  const sectorsUa = sectors.map((o) => ({ ...o, label: (MM_SECTORS[o.id] || [o.label])[0] }));
  const maps = mmHas((c) => c.maps.includes('rob'))
    ? sel('mm-map', 'Мапа', own(MM_MAPS, mm.map, 'Усі мапи', (k) => mm.all.filter((c) => c.maps.includes(k)).length)) : '';
  return `
    ${maps}
    ${sel('company-country', 'Країна', opts(s.countries, s.country, 'Усі країни'))}
    ${sel('company-sector', 'Сегмент', opts(sectorsUa, s.sector, 'Усі сегменти'))}
    ${sel('mm-use', 'Призначення', own(MM_USE, mm.use, 'Будь-яке', (k) => mm.all.filter((c) => c.use === k).length))}
    ${sel('mm-sort', 'Сортування', [['recent', 'Найсвіжіший раунд'], ['amount', 'Найбільший раунд'], ['total', 'Найбільше залучено'],
      ...(mmHas((c) => c.signal != null) ? [['signal', 'Dealroom Signal']] : []), ['name', 'За назвою']]
      .map(([k, l]) => `<option value="${k}" ${mm.sort === k ? 'selected' : ''}>${l}</option>`).join(''))}
    ${mmHas((c) => c.hot) ? `<label class="mm-check"><input type="checkbox" id="mm-hot" ${mm.hot ? 'checked' : ''}> 🔥 Гарячі</label>` : ''}
    <label class="mm-check"><input type="checkbox" id="mm-fresh" ${mm.fresh ? 'checked' : ''}> Раунд за 6 міс.</label>
    <div class="segmented small" role="group" aria-label="Вигляд">
      <button data-mm-view="map" class="${mm.view === 'map' ? 'on' : ''}">Мапа</button><button data-mm-view="table" class="${mm.view === 'table' ? 'on' : ''}">Таблиця</button>
    </div>`;
}

function companiesHtml() {
  const s = state.companies;
  if (s.error) return `<p class="sub">${esc(s.error)}</p>`;
  if (!mm.loaded) return `<p class="sub">Завантаження…</p>`;
  const list = mmFiltered();
  const body = !list.length ? '<p class="sub">Нічого не знайдено. Змініть пошук або фільтри.</p>'
    : mm.view === 'table' ? mmTableHtml(list) : mmMapHtml(list);
  return `<div class="mm-kpis">${mmKpisHtml(list)}</div>${mmMixHtml(list)}${body}
    <p class="note">Позначка <span class="mm-dot-inline"></span> — раунд за останні 6 місяців.
    ${mmHas((c) => c.hot) ? '🔥 — за Dealroom: Signal понад 80 і залучено менше $40M (рання стадія, швидке зростання). ' : ''}
    Призначення визначено автоматично за описом компанії і може помилятися. Сортування за сумою приблизне (у євро).</p>`;
}

function companiesCount() {
  if (!mm.loaded) return '';
  const n = mmFiltered().length;
  return n === mm.all.length ? `Компаній: ${n}` : `Показано ${n} з ${mm.all.length}`;
}

function companiesSectionHtml() {
  return `
      <section class="companies">
        <div style="display:flex;flex-direction:column;gap:6px">
          <h2 class="h2">Оборонні компанії Європи</h2>
          <span class="sub">Мапа за сегментами: натисніть на компанію, щоб відкрити профіль.</span>
        </div>
        <div class="searchbox">
          ${ICON_SEARCH}
          <label for="company-search" class="sr-only">Пошук компаній</label>
          <input id="company-search" value="${esc(state.companies.q)}" autocomplete="off" placeholder="Назва, технологія, місто, інвестор…">
        </div>
        <div class="company-filters" id="company-filters">${companyFiltersHtml()}</div>
        <span class="sub" id="companies-count">${companiesCount()}</span>
        <div id="companies">${companiesHtml()}</div>
        <div id="mm-drawer-box">${mmDrawerHtml()}</div>
      </section>`;
}

function paintCompanies(filters) {
  if (state.screen !== 'map') return;
  const box = document.getElementById('companies');
  if (box) box.innerHTML = companiesHtml();
  const n = document.getElementById('companies-count');
  if (n) n.textContent = companiesCount();
  const f = document.getElementById('company-filters');
  if (f && filters) f.innerHTML = companyFiltersHtml();
}


let companiesLoad = 0;
async function loadCompanies() {
  const s = state.companies;
  if (mm.loaded) return paintCompanies(true);       // уже є — лише перемалювати з новими фільтрами
  const mine = ++companiesLoad;
  s.loading = true; s.error = '';
  paintCompanies(false);
  try {
    let items = [], total = Infinity, first = null;
    for (let page = 0; items.length < total && page < 30; page++) {
      const r = await api.getCompanies({ skip: items.length, limit: MM_PAGE });
      if (mine !== companiesLoad) return;
      first ||= r;
      total = r.total ?? 0;
      if (!(r.items || []).length) break;
      items = items.concat(r.items);
    }
    s.countries = first?.countries || [];
    s.sectors = first?.sectors || [];
    const labels = Object.fromEntries(s.countries.map((o) => [o.id, o.label]));
    mm.all = items.map((c) => mmNorm(c, labels));
    mm.loaded = true; s.loading = false; s.total = mm.all.length;
    paintCompanies(true);
  } catch (e) {
    if (mine !== companiesLoad) return;
    s.loading = false;
    s.error = 'Не вдалося завантажити компанії: ' + e.message;
    paintCompanies(false);
  }
}

let companyTimer;
function onCompanySearch(value) {
  state.companies.q = value;
  clearTimeout(companyTimer);
  companyTimer = setTimeout(() => paintCompanies(false), 200);
}


function mmDrawerHtml() {
  const c = mm.open && mm.all.find((x) => x.id === mm.open);
  if (!c) return '';
  const [ua, en] = MM_SECTORS[c.sector] || [c.sectorLabel, ''];
  const site = safeUrl(c.website);
  const row = (k, v) => v ? `<dt>${k}</dt><dd>${v}</dd>` : '';
  const srcs = c.sources.filter((s) => safeUrl(s.url)).map((s) =>
    `<a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">${esc(String(s.url).replace(/^https?:\/\/(www\.)?/, '').slice(0, 70))}</a>`).join('');
  return `
    <div class="mm-shade" data-mm-close="1"></div>
    <aside class="mm-drawer" role="dialog" aria-modal="true" aria-labelledby="mm-dname" style="--sc:${mmColor(c.sector)}">
      <div class="mm-dhead"><span class="mm-mg big" aria-hidden="true">${esc(c.mg)}</span>
        <div><h3 id="mm-dname">${c.hot ? '🔥 ' : ''}${esc(c.name)}</h3>
          <div class="row"><span class="pill">${esc(ua)}</span><span class="pill">${esc(MM_USE[c.use])}</span><span class="pill">${esc(c.stage)}</span>
          ${c.fresh ? '<span class="pill mm-fresh">свіжий раунд</span>' : ''}</div></div>
        <button class="mm-close" data-mm-close="1" aria-label="Закрити">×</button></div>
      ${c.desc ? `<p class="mm-desc">${esc(c.desc)}</p>` : ''}
      <dl class="mm-dl">${row('Штаб-квартира', esc(c.loc || c.countryLabel))}${row('Заснована', esc(c.founded))}
        ${row('Сайт', site ? `<a href="${esc(site)}" target="_blank" rel="noopener noreferrer">${esc(c.website)}</a>` : '')}
        ${row('Останній раунд', esc([c.latest, c.sg === 'nd' ? '' : c.stage, c.dateText].filter(Boolean).join(' · ')))}
        ${row('Залучено всього', esc(c.total))}${row('Інвестори', esc(c.investors))}${row('Перевірка', esc(c.conf))}
        ${c.signal != null ? row('Dealroom', esc(`Signal ${c.signal}/100${c.drSegment ? ` · «${c.drSegment}»` : ''}`)) : ''}</dl>
      ${c.hot ? '<p class="note">🔥 Dealroom вважає компанію гарячою: Signal понад 80 і залучено менше $40M.</p>' : ''}
      ${c.why ? `<div><span class="section-label">Чому це важливо</span><p class="mm-desc">${esc(c.why)}</p></div>` : ''}
      ${srcs ? `<div><span class="section-label">Джерела</span><div class="mm-srcs">${srcs}</div></div>` : ''}
      ${safeUrl(c.profile) ? `<p class="note"><a href="${esc(safeUrl(c.profile))}" target="_blank" rel="noopener noreferrer">Профіль на europedefencetech.com</a>${en ? ' · ' + esc(en) : ''}</p>` : ''}
      <button class="btn-outline" data-ask="${esc(mmAskText(c))}">Запитати аналітика про ${esc(c.name)}</button>
    </aside>`;
}
// Питання до аналітика: з усім, що ми знаємо про компанію, щоб він не плутав її з тезками
// і одразу шукав і в базі, і в інтернеті, а не перепитував.
function mmAskText(c) {
  const facts = [
    `країна: ${c.loc || c.countryLabel}`,
    `сегмент: ${c.sectorLabel}`,
    c.website && `сайт: ${c.website}`,
    c.founded && `заснована: ${c.founded}`,
    c.sg !== 'nd' && `стадія: ${c.stage}`,
    c.latest && `останній раунд: ${[c.latest, c.dateText].filter(Boolean).join(', ')}`,
    c.total && `залучено всього: ${c.total}`,
    c.investors && `інвестори: ${c.investors}`,
  ].filter(Boolean).join('; ');
  return `Оборонна компанія ${c.name} (${facts}).` + (c.desc ? ` Що вона робить: ${c.desc}` : '') +
    `\nЗбери про неї коротку довідку: продукти й технології, раунди та інвестори, контракти й замовники, ` +
    `новини за останні 30 днів. Спершу пошукай у нашій базі постів (за 30 днів, за назвою компанії та її продуктами), ` +
    `потім одразу зроби веб-пошук, не перепитуючи. Шукай саме цю компанію (за сайтом і країною), не тезок. ` +
    `Дай посилання на джерела; якщо свіжих новин немає — так і скажи.`;
}
function mmOpen(id) {
  mm.open = id;
  const box = document.getElementById('mm-drawer-box');
  if (box) box.innerHTML = mmDrawerHtml();
  const close = box && box.querySelector('.mm-close');
  if (close) close.focus();
}

// Події мапи (окремо від загальних обробників: ті лише кличуть loadCompanies для країни/сегмента)
$main.addEventListener('click', (e) => {
  if (state.screen !== 'map') return;
  const t = e.target.closest('[data-mm-company],[data-mm-stage],[data-mm-view],[data-mm-sort],[data-mm-close]');
  if (!t) return;
  const d = t.dataset;
  if (d.mmCompany) return mmOpen(d.mmCompany);
  if (d.mmClose) return mmOpen(null);
  if (d.mmStage) { mm.stages.has(d.mmStage) ? mm.stages.delete(d.mmStage) : mm.stages.add(d.mmStage); return paintCompanies(false); }
  if (d.mmSort) { mm.sort = d.mmSort; return paintCompanies(true); }
  if (d.mmView) { mm.view = d.mmView; try { localStorage.setItem('mmView', mm.view); } catch (err) { /* не критично */ } return paintCompanies(true); }
});
$main.addEventListener('change', (e) => {
  if (state.screen !== 'map') return;
  const id = e.target.id;
  if (id === 'mm-map') mm.map = e.target.value;
  else if (id === 'mm-use') mm.use = e.target.value;
  else if (id === 'mm-sort') mm.sort = e.target.value;
  else if (id === 'mm-hot') mm.hot = e.target.checked;
  else if (id === 'mm-fresh') mm.fresh = e.target.checked;
  else return;
  paintCompanies(false);
});
$main.addEventListener('keydown', (e) => {
  if (state.screen !== 'map') return;
  if (e.key === 'Escape' && mm.open) return mmOpen(null);
  const row = e.target.closest && e.target.closest('tr[data-mm-company]');
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); mmOpen(row.dataset.mmCompany); }
});

const safeUrl = (u) => {
  const v = String(u || '').trim();
  if (!v) return '';
  if (/^https?:\/\//i.test(v)) return v;
  return /^[\w.-]+\.[a-z]{2,}(\/|$)/i.test(v) ? 'https://' + v : '';   // «fieldmade.com» → https://fieldmade.com
};

// ---------- Агенти (лише для адміністраторів) ----------
const RUN_STATUS = { done: 'готово', failed: 'помилка', skipped: 'пропущено', expired: 'обірвано', running: 'працює', queued: 'у черзі' };
const KIND_NAME = { goal: 'цілі', topic: 'теми', advice: 'поради' };
const num = (n) => Number(n || 0).toLocaleString('uk-UA');

function agentHtml(a) {
  const runs = Object.entries(a.runs || {}).map(([k, n]) => `${RUN_STATUS[k] || k} ${n}`).join(' · ') || 'запусків не було';
  const idle = !Object.keys(a.runs || {}).length && !a.last;    // ніколи не запускався: імовірно, вимкнений
  const last = a.last ? `<p class="sub">Останній: ${esc(RUN_STATUS[a.last.status] || a.last.status)}, ${esc(when(a.last.at))}${a.last.outcome ? ' — ' + esc(a.last.outcome) : ''}</p>` : '';
  const fails = (a.failures || []).map((f) => `<li><span class="sub">${esc(when(f.at))}</span> ${esc(f.error)}</li>`).join('');
  return `
    <article class="story agent ${a.paused ? 'paused' : ''}">
      <div class="head-row">
        <div>
          <h2>${esc(a.label)}</h2>
          <span class="sub">${esc(a.name)} · ${a.per_user ? 'для кожного користувача' : 'один на всіх'}${a.paused ? ' · <b>на паузі</b>' : ''}</span>
        </div>
        <div class="row">
          <button class="btn-outline" data-agent-run="${esc(a.name)}">${a.per_user ? 'Запустити для мене' : 'Запустити зараз'}</button>
          <button class="btn-text" data-agent-pause="${esc(a.name)}" data-paused="${a.paused ? '0' : '1'}">${a.paused ? 'Зняти з паузи' : 'Пауза'}</button>
        </div>
      </div>
      <p>${esc(runs)}; викликів моделі ${num(a.model_calls)}, токенів ${num(a.tokens_in)} + ${num(a.tokens_out)}${a.web_searches ? `, пошуків ${num(a.web_searches)}` : ''}</p>
      <p class="sub">Сьогодні: ${num(a.today.runs)} запусків, ${num(a.today.tokens)} токенів${a.today.web_searches ? `, ${num(a.today.web_searches)} пошуків` : ''}</p>
      ${last}
      ${fails ? `<details><summary class="sub">Помилки (${(a.failures || []).length})</summary><ul class="plain">${fails}</ul></details>` : ''}
      ${idle ? `<p class="sub">Немає запусків за період: можливо, на MCP-сервісі вимкнено ${esc(a.enabled_by)}.</p>` : ''}
    </article>`;
}

function renderAgents() {
  const g = state.agents;
  const days = chipsHtml([[1, 'Доба'], [7, 'Тиждень'], [30, 'Місяць']], g.days, 'agent-days');
  let body = '<p class="sub">Завантаження…</p>';
  if (g.error) body = `<p class="sub">${esc(g.error)}</p>`;
  else if (g.data) {
    const d = g.data;
    const props = (d.proposals || []).length ? `<ul class="plain">${d.proposals.map((p) =>
      `<li><b>${esc(p.agent)}</b> · ${esc(KIND_NAME[p.kind] || p.kind)}: зроблено ${p.made}; прийнято ${p.accepted}, відхилено ${p.dismissed}, застаріло ${p.stale}, відкрито ${p.open}${p.accept_rate != null ? ` — <b>${Math.round(p.accept_rate * 100)}%</b> прийнято` : ''}</li>`).join('')}</ul>`
      : '<span class="sub">За період пропозицій не було.</span>';
    const c = d.chat || {};
    const queue = (d.queue || []).length ? `<ul class="plain">${d.queue.map((q) =>
      `<li>${esc(q.agent)} <span class="sub">· ${esc(q.scope)} · ${esc(q.trigger || '')} · ${esc(when(q.not_before))}</span></li>`).join('')}</ul>`
      : '<span class="sub">Черга порожня.</span>';
    const w = d.worker || { stale: true };
    const beat = w.at ? `Воркер: останній сигнал ${esc(when(w.at))}${w.host ? ` <span class="sub">· ${esc(w.host)}</span>` : ''}` : 'Воркер: сигналу ще не було';
    const worker = `<section class="card worker"><h2>${beat}</h2>${w.stale
      ? `<p class="notice static" role="status">${w.at ? 'Понад 3 хвилини без сигналу: воркер, імовірно, зупинився.' : 'Окремий воркер не запущений: агенти працюють лише там, де їх увімкнено на MCP-сервісі.'}</p>`
      : '<p class="sub">Працює: сигнал щохвилини.</p>'}</section>`;
    body = `
      ${g.note ? `<p class="notice static" role="status">${esc(g.note)}</p>` : ''}
      ${worker}
      <section class="card">
        <h2>Чат аналітика</h2>
        <p>${num(c.replies)} відповідей, ${num(c.users)} користувачів; токенів Sonnet ${num(c.input_tokens)} + ${num(c.output_tokens)} (з кешу ${num(c.cache_read_tokens)}), веб-дослідження ${num(c.research_input_tokens)} + ${num(c.research_output_tokens)}, пошуків ${num(c.web_searches)}</p>
      </section>
      <section class="card"><h2>Пропозиції агентів</h2>${props}</section>
      <section class="card"><h2>Черга</h2><p class="sub">Зараз працює: ${num(d.running)}. Запуск із консолі починається протягом хвилини, якщо агент увімкнений на MCP-сервісі.</p>${queue}</section>
      ${(d.agents || []).map(agentHtml).join('')}`;
  }
  return `
    <div class="page"><div class="page-inner">
      <div class="head-row">
        <div>
          <h1 class="h1">Агенти</h1>
          <span class="sub">Що агенти зробили, як відповідали на їхні пропозиції, скільки це коштувало. Бачать лише адміністратори.</span>
        </div>
      </div>
      <div class="row"><span class="filter-label">Період</span>${days}</div>
      ${body}
    </div></div>`;
}

let agentsLoad = 0;   // лише найновіша відповідь: повільніша давніша не перезапише свіжу (після паузи чи запуску)

async function loadAgents() {
  const g = state.agents;
  const mine = ++agentsLoad;
  g.error = '';
  try { const data = await api.getAgents(g.days); if (mine === agentsLoad) g.data = data; }
  catch (e) { if (mine === agentsLoad) g.error = 'Не вдалося завантажити: ' + e.message; }
  if (mine === agentsLoad && state.screen === 'agents') render();
}

async function agentAction(run, note) {
  try { const r = await run(); state.agents.note = typeof note === 'function' ? note(r) : note; }
  catch (e) { state.agents.note = 'Не вдалося: ' + e.message; }
  await loadAgents();
}

// ---------- Користувач ----------
async function loadMe() {
  try {
    const me = await api.me();
    const name = me.name || me.email || 'Ви';
    document.querySelector('.user-name').textContent = name;
    document.querySelector('.user-role').textContent = me.dev ? 'локальний режим' : (me.email || '');
    document.querySelector('.avatar').textContent = name.trim().charAt(0).toUpperCase();
    document.getElementById('nav-agents').hidden = !me.admin;
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
  const form = e.target.closest('form');
  if (!form) return;
  e.preventDefault();
  if (form.dataset.form === 'chat') return ask(form.q.value);
  if (form.dataset.comment) return onComment(form.dataset.comment, form.text.value);
  if (form.dataset.reason) return answerProposal(form.dataset.reason, 'dismiss', form.reason.value.trim());
  if (form.dataset.form === 'goal') {
    const text = form.text.value.trim();
    if (text) profileAction(() => api.setGoal('add', text), `Ціль додано: «${text}».`);
  }
});

// $main.addEventListener('input', (e) => {
//   if (e.target.id === 'post-search') onSearchInput(e.target.value);
// });
$main.addEventListener('input', (e) => {
  if (e.target.id === 'post-search') onSearchInput(e.target.value);
  if (e.target.id === 'company-search') onCompanySearch(e.target.value);
});

$main.addEventListener('change', (e) => {
  if (e.target.id === 'company-country') { state.companies.country = e.target.value; loadCompanies(false); }
  if (e.target.id === 'company-sector') { state.companies.sector = e.target.value; loadCompanies(false); }
});

$main.addEventListener('click', async (e) => {
  const el = e.target.closest('button');
  if (!el) return;
  const d = el.dataset;

  if (d.action === 'stop') return stopAnswer();
  if (d.mapActivate) { el.hidden = true; return; }
    if (d.chatDelete) {
    if (d.chatDelete === 'yes') return deleteChat();
    state.chat.confirmDelete = d.chatDelete === 'ask';
    state.chat.deleteError = '';
    return render();
  }
  if (d.agentDays) { state.agents.days = +d.agentDays; state.agents.note = ''; render(); return loadAgents(); }
  if (d.agentRun) return agentAction(() => api.runAgent(d.agentRun), (r) => `Запуск ${d.agentRun} у черзі (для: ${r.for}).`);
  if (d.agentPause) {
    const on = d.paused === '1';
    return agentAction(() => api.pauseAgent(d.agentPause, on), `${d.agentPause}: ${on ? 'на паузі' : 'знято з паузи'}.`);
  }
  if (d.rate) return onRate(d.rate, d.reaction);
  if (d.fullText) return toggleFullText(d.fullText);
  if (d.go) return go(d.go);
  if (d.ask) {
    if (typeof mm !== 'undefined') mm.open = null;
    let item = null;
    try { item = d.item ? JSON.parse(d.item) : null; } catch (err) { item = null; }
    if (item && !state.chat.loading) newChat();   // питання про пункт дайджесту — нова тема: без історії іншого чату
    return ask(d.ask, item);
  }

  // пропозиції
  if (d.yes) return answerProposal(d.yes, 'accept');
  if (d.no) { state.proposals.reasonFor = d.no; render(); const i = $main.querySelector('form[data-reason] input'); if (i) i.focus(); return; }
  if (d.cancelReason) { state.proposals.reasonFor = null; return render(); }

  // профіль
  if (d.goalRemove) return profileAction(() => api.setGoal('remove', d.goalRemove), `Ціль прибрано: «${d.goalRemove}».`);
  if (d.topic) return profileAction(() => api.setTopic(d.topic, d.topicAction), (r) => r.confirmation || 'Збережено.');
  if (d.notMe) return profileAction(() => api.notInterested(d.notMe), correctedNote);
  if (d.prefsCards) return profileAction(() => api.setPrefs({ cards: +d.prefsCards }), 'Збережено.');
  if (d.prefsKind) {
    const off = state.profile?.agent_prefs?.off || [];
    const change = off.includes(d.prefsKind) ? { turn_on: [d.prefsKind] } : { turn_off: [d.prefsKind] };
    return profileAction(() => api.setPrefs(change), 'Збережено.');
  }
  if (d.prefsMentions) return profileAction(() => api.setPrefs({ mentions: d.prefsMentions === '1' }), 'Збережено.');
  if (d.push) return setPush(d.push === '1');
  if (d.investDigest) { state.digest.side = 'both'; state.digest.unseen = false; state.digest.jump = 'invest'; return go('digests'); }
  if (d.unseenDigest) { state.digest.unseen = d.unseenDigest === '1'; return loadDigest(); }
  if (d.sideDigest) { state.digest.side = d.sideDigest; return loadDigest(); }
  if (d.searchSide) { state.search.side = d.searchSide; render(); return loadSearch(); }
  if (d.days) { state.search.days = +d.days; render(); return loadSearch(); }
});

$main.addEventListener('toggle', (e) => {
  if (e.target.id !== 'map-fold') return;
  mapOpen = e.target.open;
  try { localStorage.setItem('mapOpen', mapOpen ? '1' : '0'); } catch (err) { /* не критично */ }
}, true);


loadMe();
loadRecent();
api.getProposals().then((r) => setBadge((r.proposals || []).length)).catch(() => {});
// клік по сповіщенню: нова вкладка приходить з #digests, відкрита отримує повідомлення від sw.js
if (PUSH_SUPPORTED) {
  navigator.serviceWorker.addEventListener('message', (e) => { if (e.data?.open === 'digests') go('digests'); });
}
go(location.hash === '#digests' ? 'digests' : 'chat');