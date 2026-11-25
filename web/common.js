const $ = (s, r = document) => r.querySelector(s);

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const k = (n) => n >= 1e9 ? (n / 1e9).toFixed(2) + "B"
  : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + "M"
  : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);

const state = (p, warn, stop) => (p < warn ? "ok" : p < stop ? "warn" : "bad");

function ago(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "min";
  if (sec < 86400) return Math.floor(sec / 3600) + "h" + String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
  return Math.floor(sec / 86400) + "d";
}

function until(iso) {
  if (!iso) return "-";
  const d = (new Date(iso).getTime() - Date.now()) / 1000;
  if (isNaN(d)) return "-";
  return d <= 0 ? "agora" : ago(d);
}

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function openWindow(id) {
  await fetch("/api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(id ? { id } : {}),
  });
}

const shortLabel = (l) => (l.match(/\d+[hd]/) || [l.split(" ").pop()])[0];

// limites do plano numa linha só: 5h ▬▬▬ 11% ↻3h48
function limitsInline(usage) {
  if (!usage.windows.length) return `<span class="faint">limites indisponíveis</span>`;
  return usage.windows.map((w) => `
    <span class="lim ${state(w.pct, 70, 90)}" title="${esc(w.label)}">
      <em>${esc(shortLabel(w.label))}</em>
      <span class="bar"><i style="width:${Math.min(100, w.pct)}%"></i></span>
      <span class="pct">${w.pct.toFixed(0)}%</span>
      <small>↻${until(w.resets_at)}</small>
    </span>`).join("");
}

function usageNote(usage) {
  const retry = usage.err && usage.retry_in != null ? ` · nova tentativa em ${ago(usage.retry_in)}` : "";
  if (usage.live && !usage.err) return `ao vivo · ${ago(usage.age)}`;
  if (usage.windows.length) return `dados de ${ago(usage.age)} atrás` + (usage.err ? ` · ${usage.err}${retry}` : "");
  return usage.err ? `${usage.err}${retry}` : "buscando…";
}

function modelName(ctx) { return ctx.model_name || ctx.model; }

/* ---------- tema (claro/escuro) e barra de título própria ---------- */
const THEME_KEY = "ccwatch-theme";
function systemTheme() { return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"; }
function applyTheme() {
  let t = null;
  try { t = localStorage.getItem(THEME_KEY); } catch {}
  document.documentElement.dataset.theme = t || systemTheme();
  const m = document.querySelector('meta[name="theme-color"]');
  if (m) m.content = document.documentElement.dataset.theme === "light" ? "#eef1f7" : "#0b0d13";
}
applyTheme();
addEventListener("storage", applyTheme);

const ICONS = {
  sun: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M12.6 3.4l-1 1M4.4 11.6l-1 1"/></svg>',
  moon: '<svg viewBox="0 0 16 16"><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z"/></svg>',
};

// botão de tema dentro de um contêiner da página
function mountThemeButton(host) {
  const b = document.createElement("button");
  b.className = "theme-btn";
  b.title = "Alternar tema claro/escuro";
  const paint = () => { b.innerHTML = document.documentElement.dataset.theme === "light" ? ICONS.moon : ICONS.sun; };
  b.onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    try { localStorage.setItem(THEME_KEY, next); } catch {}
    applyTheme(); paint();
    document.dispatchEvent(new Event("themechange"));
  };
  paint();
  addEventListener("storage", () => { paint(); document.dispatchEvent(new Event("themechange")); });
  host.appendChild(b);
}
