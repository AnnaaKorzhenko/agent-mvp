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

  // POST /chat — з'явиться на кроці 5.2 (docs/WEB_PLAN.md)
  async askAnalyst(messages) {
    if (USE_MOCK) { await delay(700); return { content: 'Демо-відповідь.', sources: [] }; }
    return http('/chat', { method: 'POST', body: JSON.stringify({ messages }) });
  },

  // GET /profile -> { goals: [..], topics: [{ name, status, expires_at, ... }], top_tags: [{ tag, score }],
  //                   top_categories: [...], categories_today: {...}, about_user: { kind: [text] } }
  async getProfile() {
    if (USE_MOCK) return { goals: [], topics: [], top_tags: [], top_categories: [], about_user: {} };
    return http('/profile');
  }
};
