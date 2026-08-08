/* The result card: audio element, stats, downloads, and the subtitle view. */

import { $, el, formatBytes, formatDuration, formatNumber } from "./ui.js";

export class Player {
  constructor() {
    this.panel = $("#result-panel");
    this.audio = $("#player");
    this.stats = $("#result-stats");
    this.downloads = $("#download-row");
    this.preparedPanel = $("#tab-prepared");
    this.cuesPanel = $("#tab-cues");
    this.current = null;
    this._wireTabs();
  }

  _wireTabs() {
    for (const tab of document.querySelectorAll(".result-tabs .tab")) {
      tab.addEventListener("click", () => {
        for (const other of document.querySelectorAll(".result-tabs .tab")) {
          other.classList.toggle("is-active", other === tab);
        }
        this.preparedPanel.hidden = tab.dataset.tab !== "prepared";
        this.cuesPanel.hidden = tab.dataset.tab !== "cues";
      });
    }
  }

  /** Show a generation - both a fresh one and one replayed from history. */
  show(generation, { autoplay = false } = {}) {
    this.current = generation;
    this.panel.hidden = false;

    this.audio.src = generation.urls.audio;
    this._attachCaptions(generation);
    this.audio.load();
    if (autoplay) {
      this.audio.play().catch(() => {
        /* autoplay can be blocked; the controls still work */
      });
    }

    this._renderStats(generation);
    this._renderDownloads(generation);
    this.preparedPanel.textContent = generation.prepared_text || generation.text || "";
    this._renderCues(generation);
    this.panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  _attachCaptions(generation) {
    for (const track of Array.from(this.audio.querySelectorAll("track"))) track.remove();
    if (!generation.urls.vtt) return;
    this.audio.append(
      el("track", {
        kind: "captions",
        src: generation.urls.vtt,
        srclang: "he",
        label: "עברית",
        default: true,
      })
    );
  }

  _renderStats(generation) {
    // Prefer the element's own duration once metadata loads; the cue-derived
    // value ignores trailing silence and reads slightly short.
    const showDuration = (seconds) => {
      const node = $("#stat-duration");
      if (node) node.textContent = formatDuration(seconds);
    };
    this.audio.onloadedmetadata = () => {
      if (Number.isFinite(this.audio.duration)) showDuration(this.audio.duration);
    };

    this.stats.replaceChildren(
      el("div", {}, [
        el("dt", { text: "אורך" }),
        el("dd", { id: "stat-duration", text: formatDuration(generation.duration) }),
      ]),
      el("div", {}, [
        el("dt", { text: "תווים" }),
        el("dd", { text: formatNumber(generation.char_count) }),
      ]),
      el("div", {}, [
        el("dt", { text: "גודל" }),
        el("dd", { text: formatBytes(generation.audio_bytes) }),
      ])
    );
  }

  _renderDownloads(generation) {
    const links = [["MP3", `${generation.urls.audio}?download=1`, true]];
    if (generation.urls.srt) links.push(["SRT", generation.urls.srt, false]);
    if (generation.urls.vtt) links.push(["VTT", generation.urls.vtt, false]);

    this.downloads.replaceChildren(
      ...links.map(([label, href, primary]) =>
        el("a", {
          class: `btn ${primary ? "btn-primary" : "btn-ghost"}`,
          href,
          download: "",
          text: `הורדת ${label}`,
        })
      )
    );
  }

  async _renderCues(generation) {
    if (!generation.urls.vtt) {
      this.cuesPanel.textContent = "לא נוצרו כתוביות עבור קובץ זה.";
      return;
    }
    this.cuesPanel.textContent = "טוען כתוביות…";
    try {
      const response = await fetch(generation.urls.vtt, { credentials: "same-origin" });
      const cues = parseVtt(await response.text());
      this.cuesPanel.replaceChildren(
        el(
          "ul",
          { class: "cue-list" },
          cues.map((cue) =>
            el("li", {}, [
              el("span", { class: "cue-time", text: formatDuration(cue.start) }),
              el("span", { text: cue.text }),
            ])
          )
        )
      );
    } catch (error) {
      this.cuesPanel.textContent = "לא ניתן לטעון את הכתוביות.";
    }
  }
}

function toSeconds(stamp) {
  const [hours, minutes, rest] = stamp.split(":");
  return Number(hours) * 3600 + Number(minutes) * 60 + Number(rest.replace(",", "."));
}

/** Minimal WebVTT parser - enough for the cue list we generate ourselves. */
export function parseVtt(source) {
  const cues = [];
  for (const block of source.split(/\r?\n\r?\n/)) {
    const lines = block.trim().split(/\r?\n/);
    const timing = lines.find((line) => line.includes("-->"));
    if (!timing) continue;
    const [start, end] = timing.split("-->").map((part) => part.trim());
    const text = lines.slice(lines.indexOf(timing) + 1).join(" ").trim();
    if (text) cues.push({ start: toSeconds(start), end: toSeconds(end), text });
  }
  return cues;
}
