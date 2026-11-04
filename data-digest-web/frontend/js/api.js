// Шар доступу до даних: API «мозку» (brain/web/app.py), той самий сервер, що віддає інтерфейс.
// Користувач визначається сесією (вхід через Auth0), ніколи не параметрами запиту.
// USE_MOCK = true — демо-дані з data.js без бекенду (лише для верстки).

const API_BASE = '/api';
const USE_MOCK = false;

async function http(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Brain': '1', ...(options.headers || {}) }
  });
  if (res.status === 401) {                       // сесії немає або вона скінчилась — на вхід
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.hash);
    throw new Error('Потрібно увійти');
  }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch (e) { /* не JSON */ }
    throw new Error(typeof detail === 'string' && detail ? detail : `Помилка сервера (${res.status})`);
  }
  return res.json();
}

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

window.api = {
  // GET /me -> { name, email, picture, dev }
  async me() {
    if (USE_MOCK) return { name: 'Демо', email: '', picture: null, dev: true };
    return http('/me');
  },

  // GET /digest?side=both|ua|ru|topics[&date=YYYY-MM-DD]
  // -> { date, digests: [{ side, side_note, digest_id, sections: [{ category, title, items: [
  //        { ref, n, text, source_urls, source_post_ids, is_russian_source_claim, why: [..], relevance }] }] }],
  //      missing_sides, note, proposals: [...], topics_to_renew: [...] }
  async getDigest(side, date) {
    if (USE_MOCK) { await delay(150); return { date: null, digests: [], missing_sides: [], note: 'Демо-режим' }; }
    const p = new URLSearchParams({ side });
    if (date) p.set('date', date);
    return http(`/digest?${p}`);
  },

  // GET /posts?q=...&side=all|ua|ru&days=1..30
  // -> { query, results: [{ ref, post_id, summary, tags, category, source_side, is_russian_source_claim,
  //        channel: { username, country }, post_url, published_at, importance, why }], note }
  async searchPosts(q, side, days) {
    if (USE_MOCK) { await delay(150); return { query: q, results: [] }; }
    return http(`/posts?${new URLSearchParams({ q, side, days })}`);
  },

  // ---- Чат з аналітиком (brain/web/chat.py). Історію зберігає сервер, клієнт шле лише нове повідомлення.

  // POST /chats -> { id }  (нічого не зберігається до першого повідомлення)
  async newChat() {
    if (USE_MOCK) return { id: 'demo' };
    return http('/chats', { method: 'POST' });
  },

  // GET /chats -> { chats: [{ id, title, updated_at }] }, найновіші першими
  async listChats() {
    if (USE_MOCK) return { chats: [] };
    return http('/chats');
  },

  // GET /chats/:id -> { id, messages: [{ id, role, content, created_at }] }
  async getChat(id) {
    if (USE_MOCK) return { id, messages: [] };
    return http(`/chats/${encodeURIComponent(id)}`);
  },

  // POST /chats/:id/messages { content, item? } -> потік server-sent events, по одному JSON у рядку `data: …`:
  //   { type: 'text', text } | { type: 'tool', name } | { type: 'done', message_id, stop_reason } | { type: 'error', detail }
  // onEvent викликається для кожної події; signal (AbortController) зупиняє відповідь.
  // item: { digest_id, item_n } — пункт дайджесту, про який питає кнопка (його id отримує модель).
  async sendMessage(id, content, onEvent, signal, item) {
    if (USE_MOCK) { await delay(500); onEvent({ type: 'text', text: 'Демо-відповідь.' }); onEvent({ type: 'done' }); return; }
    const res = await fetch(`${API_BASE}/chats/${encodeURIComponent(id)}/messages`, {
      method: 'POST', credentials: 'same-origin', signal,
      headers: { 'Content-Type': 'application/json', 'X-Brain': '1' },
      body: JSON.stringify(item ? { content, item } : { content })
    });
    if (res.status === 401) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname + location.hash);
      throw new Error('Потрібно увійти');
    }
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail; } catch (e) { /* не JSON */ }
      throw new Error(typeof detail === 'string' && detail ? detail : `Помилка сервера (${res.status})`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        for (const line of chunk.split('\n')) {
          if (line.startsWith('data: ')) onEvent(JSON.parse(line.slice(6)));
        }
      }
    }
  },

  // GET /profile -> { goals: [..], topics: [{ name, status, expires_at, ... }], top_tags: [{ tag, score }],
  //                   top_categories: [...], categories_today: {...}, about_user: { kind: [text] },
  //                   agent_prefs: { cards, off: [kind], mentions } }
  async getProfile() {
    if (USE_MOCK) return { goals: [], topics: [], top_tags: [], top_categories: [], about_user: {}, agent_prefs: { cards: 3, off: [], mentions: true } };
    return http('/profile');
  },

  // ---- Кнопки: кожна виконує один інструмент «мозку» (як кнопки карток у Claude) ----

  // GET /proposals -> { proposals: [{ proposal_id, kind: goal|topic|advice, title, why, yes, links, ask }] }
  async getProposals() {
    if (USE_MOCK) return { proposals: [] };
    return http('/proposals');
  },

  // POST /proposals/:id { answer: accept|dismiss, reason? } -> { accepted, done, ask }  (ask: запит до аналітика)
  async answerProposal(id, answer, reason) {
    return http(`/proposals/${encodeURIComponent(id)}`, { method: 'POST', body: JSON.stringify({ answer, reason: reason || null }) });
  },

  // POST /goals { action: add|remove, text } -> { goals: [..] }
  async setGoal(action, text) {
    return http('/goals', { method: 'POST', body: JSON.stringify({ action, text }) });
  },

  // POST /topics { name, action: follow|stop } -> { topic, confirmation }  (follow — продовжити ще на 30 днів)
  async setTopic(name, action) {
    return http('/topics', { method: 'POST', body: JSON.stringify({ name, action }) });
  },

  // POST /profile/not-interested { text } -> { dropped_interests, dropped_intents, closed_proposals, followed_matching }
  async notInterested(text) {
    return http('/profile/not-interested', { method: 'POST', body: JSON.stringify({ text }) });
  },

  // POST /prefs { cards?, turn_off?, turn_on?, mentions? } -> { cards, off, mentions }
  async setPrefs(change) {
    return http('/prefs', { method: 'POST', body: JSON.stringify(change) });
  },

  // POST /feedback { reaction?, target_type: post|digest_item, post_id | digest_id + item_n, text? } -> { confirmation }
  async rate(target, reaction, text) {
    return http('/feedback', { method: 'POST', body: JSON.stringify({ ...target, reaction: reaction || null, text: text || null }) });
  },

  // ---- Консоль агентів (лише ADMIN_EMAILS; brain/web/admin.py) ----

  // GET /admin/agents?days=7 -> { agents: [{ name, label, per_user, enabled_by, runs: {status: n}, model_calls,
  //   tokens_in, tokens_out, web_searches, last: { status, outcome, at }, failures: [{ at, error }],
  //   today: { runs, tokens, web_searches }, paused: { by, since } | null }], queue: [...], running,
  //   proposals: [{ agent, kind, made, accepted, dismissed, stale, open, accept_rate }], chat: { replies, users, ... } }
  async getAgents(days) {
    return http(`/admin/agents?days=${encodeURIComponent(days)}`);
  },

  async pauseAgent(name, paused) {
    return http(`/admin/agents/${encodeURIComponent(name)}/pause`, { method: 'POST', body: JSON.stringify({ paused }) });
  },

  // -> { queued: true, for: 'усі' | 'вас' }
  async runAgent(name) {
    return http(`/admin/agents/${encodeURIComponent(name)}/run`, { method: 'POST' });
  },

  // POST /chats/:id/messages/:mid/rating { reaction, text? } — оцінка відповіді аналітика
  async rateReply(chatId, messageId, reaction, text) {
    return http(`/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(messageId)}/rating`,
      { method: 'POST', body: JSON.stringify({ reaction, text: text || null }) });
  }
};
