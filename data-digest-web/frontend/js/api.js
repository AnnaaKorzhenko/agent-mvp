// Шар доступу до даних.
// Поки USE_MOCK = true, все працює на демо-даних з data.js.
// Коли з'явиться бекенд (напр. FastAPI поруч з ботом, що читає MongoDB "News"),
// постав USE_MOCK = false і API_BASE на його адресу. Очікувані ендпоінти описані нижче.

const API_BASE = '/api';   // фронт і бекенд на одному сервері
const USE_MOCK = false;  // true — демо-дані без бекенду

async function http(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

window.api = {
  // GET /digest?period=day|week&category=all|tech|front|invest
  // -> [{ id, title, summary, category, side, relevance: high|medium|low, posts_count }]
  async getDigest(period, category) {
    if (!USE_MOCK) return http(`/digest?period=${encodeURIComponent(period)}&category=${encodeURIComponent(category)}`);
    await delay(150);
    return DEMO.stories
      .filter((s) => period === 'week' || s.period === 'day')
      .filter((s) => category === 'all' || s.category === category);
  },

  // GET /posts?q=...&platform=all|Telegram|X|Web&lang=all|UA|RU|EN
  // -> [{ id, platform, source, lang, date, category, summary, url }]
  async searchPosts(q, platform, lang) {
    if (!USE_MOCK) {
      const p = new URLSearchParams({ q, platform, lang });
      return http(`/posts?${p}`);
    }
    await delay(150);
    const query = q.trim().toLowerCase();
    return DEMO.posts
      .filter((p) => platform === 'all' || p.platform === platform)
      .filter((p) => lang === 'all' || p.lang === lang)
      .filter((p) => !query || (p.summary + ' ' + p.source).toLowerCase().includes(query));
  },

  // POST /chat  body: { messages: [{ role: 'user'|'assistant', content }] }
  // -> { content, sources: [{ platform, name, url }] }
  async askAnalyst(messages) {
    if (!USE_MOCK) return http('/chat', { method: 'POST', body: JSON.stringify({ messages }) });
    await delay(700);
    return {
      content:
        'За останні 7 днів у базі є кілька сюжетів на цю тему.\n\n' +
        '1. [Сюжет 1] — коротко, що сталося і хто про це повідомив.\n' +
        '2. [Сюжет 2] — як це подають українські та російські канали.\n' +
        '3. [Сюжет 3] — що це може означати для оборонних інвестицій.\n\n' +
        'Хочете розгорнути будь-який сюжет або порівняти наративи UA та RU?',
      sources: [
        { platform: 'TG', name: '[канал UA]', url: '#' },
        { platform: 'TG', name: '[канал RU]', url: '#' },
        { platform: 'X', name: '[акаунт]', url: '#' },
        { platform: 'Web', name: '[сайт]', url: '#' }
      ]
    };
  },

  // GET /profile -> { interests: [id], schedule: daily|weekly|off, sides: ['UA','RU','Світ'] }
  async getProfile() {
    if (!USE_MOCK) return http('/profile');
    return structuredClone(DEMO.profile);
  },

  // PUT /profile  body: той самий об'єкт
  async saveProfile(profile) {
    if (!USE_MOCK) return http('/profile', { method: 'PUT', body: JSON.stringify(profile) });
    await delay(200);
    DEMO.profile = structuredClone(profile);
    return { ok: true };
  }
};
