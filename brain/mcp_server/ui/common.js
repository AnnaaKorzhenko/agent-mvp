// Shared by every widget (inlined by brain/mcp_server/widgets.py ahead of the page's own script).
// A card shows one rated thing with 👍 / 👎 / 50/50; a press calls record_feedback through the host, then
// offers «Чому?». Every rating made in the widget is sent to the model as context. 🔎 puts «Глибше про #N …» into
// the chat as the user's message: the model then runs deep_dive and searches the web (only it can). 📌 puts
// «Відстежувати тему з #N …»: the model names the topic in three languages and calls track_topic.
const { App, applyDocumentTheme, applyHostStyleVariables, applyHostFonts } = globalThis.__mcpExtApps;

const REACTIONS = [["useful", "👍"], ["not_useful", "👎"], ["neutral", "50/50"]];
const LABEL = { useful: "👍 корисно", not_useful: "👎 не корисно", neutral: "50/50" };
const TRACKED = "тема, яку ви відстежуєте: ";      // the server's `why` for an item naming a followed topic
const root = document.getElementById("root");

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);   // strings become text nodes: no HTML injection
  return node;
}

function applyContext(ctx) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}

function short(text, n = 70) { return text.length > n ? text.slice(0, n - 1) + "…" : text; }

function host(url) { try { return new URL(url).host + new URL(url).pathname; } catch { return url; } }

function payload(result) {
  if (result?.structuredContent) return result.structuredContent;
  try { return JSON.parse(result?.content?.[0]?.text ?? ""); } catch { return null; }
}

// "Для вас: …": why an item matters to this user (its `why` from the server), or nothing.
function whyLine(why) {
  return why?.length ? el("div", { class: "why" }, "Для вас: " + why.join(" · ")) : null;
}

function errorText(result, fallback) {
  return result?.content?.find?.((c) => c.type === "text")?.text || fallback;
}

// name: the app's name for the host; where: «у віджеті …», used in the context sent to the model.
function createWidget({ name, where }) {
  const app = new App({ name, version: "1.0.0" }, {}, { autoResize: true });
  const ratings = new Map();      // card key -> {ref, text, reaction, feedback_id, comment}

  // The model sees only the latest context update, so always send every rating made in this widget.
  async function syncModelContext() {
    const lines = [...ratings.values()].map((r) =>
      `- #${r.ref} «${short(r.text)}»: ${LABEL[r.reaction]}` + (r.comment ? `; коментар: «${r.comment}»` : "") +
      ` (feedback_id ${r.feedback_id})`);
    const text = `Оцінки, які користувач поставив кнопками ${where} (уже збережено, record_feedback повторно ` +
                 "не викликати; коментар до оцінки можна додати через її feedback_id):\n" + lines.join("\n");
    try { await app.updateModelContext({ content: [{ type: "text", text }] }); } catch { /* host may not support it */ }
  }

  function link(url, label = host(url)) {
    return el("a", { href: url, target: "_blank", rel: "noopener", onclick: async (event) => {
      event.preventDefault();
      try { await app.openLink({ url }); } catch { window.open(url, "_blank", "noopener"); }
    } }, label);
  }

  // key: unique per rated thing; ref: the number the user sees; text: what the model is told was rated;
  // body: nodes above the buttons; urls: source links; target: record_feedback ids for this thing;
  // why: the server's reasons (an item already naming a followed topic shows 📌 as done).
  function card({ key, ref, text, body, urls = [], target, why = [] }) {
    const status = el("span", { class: "status", role: "status" });
    const box = el("textarea", { placeholder: "Чому? (необов'язково)", maxlength: "2000", "aria-label": "Коментар" });
    const send = el("button", { type: "button" }, "Надіслати");
    const skip = el("button", { type: "button" }, "Пропустити");
    const comment = el("div", { class: "comment", hidden: true }, box, el("div", { class: "actions" }, send, skip));
    const buttons = REACTIONS.map(([reaction, label]) => el("button", {
      type: "button", "aria-pressed": "false", "aria-label": LABEL[reaction], title: LABEL[reaction],
      onclick: () => rate(reaction),
    }, label));
    const deeper = el("button", { type: "button", class: "deeper", title: "Глибше: знайти більше, зокрема в інтернеті",
                                  "aria-label": "Глибше", onclick: () => toChat(`🔎 Глибше про #${ref} «${short(text, 120)}»`,
                                                                               `глибше про #${ref}`) }, "🔎");
    const followed = why.find((w) => w.startsWith(TRACKED))?.slice(TRACKED.length);
    const track = el("button", { type: "button", class: "deeper",
      title: followed ? `Уже відстежується: ${followed}` : "Відстежувати цю тему в наступних зведеннях",
      "aria-label": followed ? "Уже відстежується" : "Відстежувати тему", "aria-pressed": followed ? "true" : "false",
      onclick: () => toChat(`📌 Відстежувати тему з #${ref} «${short(text, 120)}»`, `відстежуй тему з #${ref}`),
    }, "📌");
    if (followed) track.disabled = true;

    function busy(on) {
      for (const b of [...buttons, deeper, send, skip]) b.disabled = on;
      track.disabled = on || Boolean(followed);
    }
    function show(message, kind = "") { status.textContent = message; status.className = "status " + kind; }

    async function rate(reaction) {
      busy(true); show("Зберігаю…");
      try {
        // record_feedback requires user_message; "[widget]" marks a button press (not typed text) in the log.
        const user_message = `[widget] ${LABEL[reaction]} #${ref} «${short(text)}»`;
        const result = await app.callServerTool({ name: "record_feedback",
                                                  arguments: { ...target, reaction, user_message } });
        const out = payload(result);
        if (result.isError || !out?.feedback_id) throw new Error(errorText(result, "Не вдалося зберегти"));
        ratings.set(key, { ref, text, reaction, feedback_id: out.feedback_id, comment: "" });
        buttons.forEach((b, i) => b.setAttribute("aria-pressed", String(REACTIONS[i][0] === reaction)));
        show("✓ Збережено", "ok");
        box.value = ""; comment.hidden = false;
        await syncModelContext();
      } catch (e) {
        show("Не вдалося зберегти: " + (e?.message || e), "error");
      } finally { busy(false); }
    }

    async function followUp(words) {
      const r = ratings.get(key);
      if (!r) return;
      busy(true);
      try {
        // A typed comment is the user's own words; skipping is a button press.
        const args = { feedback_id: r.feedback_id, ...(words ? { text: words } : {}),
                       user_message: words || `[widget] пропустив коментар до #${ref}` };
        const result = await app.callServerTool({ name: "record_feedback", arguments: args });
        if (result.isError) throw new Error(errorText(result, "Не вдалося зберегти коментар"));
        r.comment = words || "";
        comment.hidden = true;
        show(words ? "✓ Збережено, з коментарем" : "✓ Збережено", "ok");
        if (words) await syncModelContext();
      } catch (e) {
        show("Не вдалося зберегти коментар: " + (e?.message || e), "error");
      } finally { busy(false); }
    }
    // A request for the model, as the user's message. fallback: what to type if the host refuses.
    async function toChat(message, fallback) {
      busy(true);
      try {
        const result = await app.sendMessage({ role: "user", content: [{ type: "text", text: message }] });
        if (result?.isError) throw new Error("rejected");
        // Claude puts it into the message box rather than sending it: the user presses Enter.
        show("Запит у полі повідомлення: надішліть його", "ok");
      } catch {
        show(`Напишіть у чаті: «${fallback}»`, "error");
      } finally { busy(false); }
    }

    send.addEventListener("click", () => { const t = box.value.trim(); if (t) followUp(t); else box.focus(); });
    skip.addEventListener("click", () => followUp(""));

    return el("div", { class: "card", "data-ref": ref },
      el("div", { class: "row" }, el("span", { class: "ref" }, `#${ref}`),
         el("div", { class: "text" }, body,
            el("div", { class: "foot" }, el("div", { class: "links" }, urls.map((u) => link(u))),
               el("div", { class: "actions" }, status, deeper, track, buttons)))),
      comment);
  }

  // Handlers before connect(), so the first notifications are not missed.
  // A result not meant for cards (the user asked for text, or there is nothing to show; Claude writes it
  // instead) leaves the widget empty: zero height, so the host shows nothing.
  async function start(render) {
    app.ontoolresult = (result) => {
      const data = payload(result);
      if (data?.shown_in_widget === false) { root.replaceChildren(); document.body.hidden = true; return; }
      document.body.hidden = false;
      render(data);
    };
    app.onhostcontextchanged = (ctx) => applyContext(ctx);
    await app.connect();
    applyContext(app.getHostContext());
  }

  return { app, card, link, start };
}
