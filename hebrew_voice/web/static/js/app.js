/* Entry point: reads the bootstrap payload, wires the modules together,
   and owns the generate action. */

import { api, url } from "./api.js";
import { Composer } from "./composer.js";
import { History } from "./history.js";
import { Player } from "./player.js";
import { EditPanel } from "./editing.js";
import { Timeline } from "./media.js";
import { Renders } from "./renders.js";
import { $, formatNumber, toast } from "./ui.js";

const bootstrap = JSON.parse($("#bootstrap").textContent);

const composer = new Composer({
  voices: bootstrap.voices,
  maxChars: bootstrap.limits.max_chars,
});
const player = new Player();
const edit = new EditPanel({ onChange: () => renders.refresh() });
const media = new Timeline({
  /* A different set of shots, or a different order, is a different video - so
     anything already shown no longer describes what the button would make. */
  onChange: () => renders.refresh(),
  onTracks: (tracks) => edit.setTracks(tracks),
});
const renders = new Renders({
  enabled: bootstrap.rendering && bootstrap.rendering.enabled,
  maxSeconds: bootstrap.rendering && bootstrap.rendering.max_seconds,
  media,
  edit,
});
if (bootstrap.rendering && bootstrap.rendering.enabled) media.load();
const history = new History({
  voices: bootstrap.voices,
  onOpen: (generation, { autoplay }) => {
    player.show(generation, { autoplay });
    renders.show(generation);
    history.markCurrent(generation.id);
  },
  onRestore: (generation) => {
    composer.load(generation);
    window.scrollTo({ top: 0, behavior: "smooth" });
  },
});

/* -------------------------------------------------------------- quota UI */

function renderQuota(limits) {
  const { used_today: used, limit } = limits;
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
  const fill = $("#quota-fill");
  fill.style.inlineSize = `${(ratio * 100).toFixed(1)}%`;
  fill.classList.toggle("is-warn", ratio >= 0.8 && ratio < 1);
  fill.classList.toggle("is-full", ratio >= 1);
  $("#quota-text").textContent =
    `${formatNumber(used)} / ${formatNumber(limit)} תווים היום`;
}

renderQuota(bootstrap.limits);

/* -------------------------------------------------------------- generate */

const generateBtn = $("#generate");
const statusLabel = $("#generate-status");
let busy = false;

async function generate() {
  if (busy) return;
  const text = composer.text.trim();
  if (!text) {
    toast("אין טקסט להקראה", "error");
    composer.textarea.focus();
    return;
  }
  if (text.length > bootstrap.limits.max_chars) {
    toast(`הטקסט ארוך מהמותר (${formatNumber(bootstrap.limits.max_chars)} תווים)`, "error");
    return;
  }

  busy = true;
  generateBtn.disabled = true;
  generateBtn.setAttribute("aria-busy", "true");

  const started = Date.now();
  const ticker = window.setInterval(() => {
    statusLabel.textContent = `יוצר… ${Math.round((Date.now() - started) / 1000)} שניות`;
  }, 250);

  try {
    const generation = await api.synthesize(composer.payload());
    player.show(generation, { autoplay: true });
    renders.show(generation);
    history.prepend(generation);
    history.markCurrent(generation.id);
    renderQuota({ ...generation.quota, used_today: generation.quota.used_today });
    statusLabel.textContent = `מוכן תוך ${Math.round((Date.now() - started) / 1000)} שניות`;
  } catch (error) {
    statusLabel.textContent = "";
    toast(error.message, "error");
  } finally {
    window.clearInterval(ticker);
    busy = false;
    generateBtn.disabled = false;
    generateBtn.removeAttribute("aria-busy");
  }
}

generateBtn.addEventListener("click", generate);

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    generate();
  }
});

/* ---------------------------------------------------------------- chrome */

$("#theme-toggle").addEventListener("click", () => window.hvTheme.cycle());

$("#logout").addEventListener("click", async () => {
  try {
    await api.logout();
  } catch (error) {
    /* the cookie is cleared server-side either way */
  }
  window.location.href = url("/login");
});

// Refresh the preview the first time the advanced panel is opened.
$("#advanced").addEventListener("toggle", () => {
  if ($("#advanced").open) composer.refreshPreview();
});

history.load();
