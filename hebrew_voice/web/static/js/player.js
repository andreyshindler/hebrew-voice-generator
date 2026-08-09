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
    this.captionControls = $("#caption-controls");
    this.current = null;
    /* Rows and timings of the cue list, kept so the active one can be
       highlighted in time with the audio. */
    this.cueRows = [];
    this.cues = [];
    this.activeCue = -1;
    /* A view over the current recording, not a saved preference: it starts at
       whatever density the stored files were rendered at, which is what the
       composer's control chose. Persisting it here would mean the result card
       disagreeing with the files a fresh generation just wrote. */
    this.captions = { words: 7, strip: false, minDuration: 0 };
    this._wireTabs();
    this._wireCaptionControls();
    this._wireHighlight();
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

  _wireCaptionControls() {
    const words = $("#cap-words");
    const min = $("#cap-min");
    const strip = $("#cap-strip");
    if (!words) return;

    const changed = () => {
      this.captions = {
        words: Number(words.value),
        minDuration: Number(min.value),
        strip: strip.checked,
      };
      // Everything the captions feed from is re-pointed at the new URL. The
      // audio is deliberately left alone: nothing about it changed, and
      // reloading it would restart playback mid-listen.
      if (!this.current) return;
      this._attachCaptions();
      this._renderDownloads();
      this._renderCues();
    };
    for (const control of [words, min, strip]) {
      control.addEventListener("change", changed);
    }
  }

  /** Point the controls at the density this recording was stored at. */
  _resetCaptionControls(generation) {
    this.captions = {
      words: generation.words_per_cue || 7,
      strip: false,
      minDuration: 0,
    };
    if (!this.captionControls) return;
    this.captionControls.hidden = !generation.can_regroup || !generation.urls.vtt;
    $("#cap-words").value = String(this.captions.words);
    $("#cap-min").value = "0";
    $("#cap-strip").checked = false;
  }

  /* Highlighting is driven from our own parsed cues rather than the track's
     `cuechange`, which would mean waiting on the TextTrack to load and
     mapping its cues back onto the rows. */
  _wireHighlight() {
    const sync = () => this._highlight(this.audio.currentTime);
    this.audio.addEventListener("timeupdate", sync);
    this.audio.addEventListener("seeked", sync);
  }

  _highlight(time) {
    if (!this.cueRows.length) return;
    const index = this.cues.findIndex((cue) => time >= cue.start && time < cue.end);
    if (index === -1 && this.activeCue === -1) return;
    if (index === this.activeCue) return;
    if (this.activeCue >= 0) this.cueRows[this.activeCue].classList.remove("is-active");
    this.activeCue = index;
    if (index < 0) return;
    const row = this.cueRows[index];
    row.classList.add("is-active");
    if (this.cuesPanel.hidden) return;
    row.scrollIntoView({ block: "nearest" });
  }

  /** The subtitle URL for the density currently selected. */
  _subtitleUrl(kind) {
    const base = this.current && this.current.urls[kind];
    if (!base) return null;
    // Older recordings kept no word timings; only the density they were
    // rendered at is available, so ask for exactly that.
    if (!this.current.can_regroup) return base;
    // Asking for what's already on disk would make the server recompute it
    // and cost the response its immutable caching. Take the file.
    if (
      this.captions.words === (this.current.words_per_cue || 7) &&
      !this.captions.strip &&
      this.captions.minDuration <= 0
    ) {
      return base;
    }
    const params = new URLSearchParams({ words: String(this.captions.words) });
    if (this.captions.strip) params.set("strip_punctuation", "1");
    if (this.captions.minDuration > 0) {
      params.set("min_duration", String(this.captions.minDuration));
    }
    return `${base}?${params}`;
  }

  /** Show a generation - both a fresh one and one replayed from history. */
  show(generation, { autoplay = false } = {}) {
    this.current = generation;
    this.panel.hidden = false;
    this._resetCaptionControls(generation);

    this.audio.src = generation.urls.audio;
    this._attachCaptions();
    this.audio.load();
    if (autoplay) {
      this.audio.play().catch(() => {
        /* autoplay can be blocked; the controls still work */
      });
    }

    this._renderStats(generation);
    this._renderDownloads();
    this.preparedPanel.textContent = generation.prepared_text || generation.text || "";
    this._renderCues();
    this.panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  _attachCaptions() {
    for (const track of Array.from(this.audio.querySelectorAll("track"))) track.remove();
    const src = this._subtitleUrl("vtt");
    if (!src) return;
    this.audio.append(
      el("track", {
        kind: "captions",
        src,
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

  _renderDownloads() {
    const links = [["MP3", `${this.current.urls.audio}?download=1`, true]];
    for (const kind of ["srt", "vtt"]) {
      const href = this._subtitleUrl(kind);
      if (href) links.push([kind.toUpperCase(), href, false]);
    }

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

  async _renderCues() {
    this.cueRows = [];
    this.cues = [];
    this.activeCue = -1;

    const url = this._subtitleUrl("vtt");
    if (!url) {
      this.cuesPanel.textContent = "לא נוצרו כתוביות עבור קובץ זה.";
      return;
    }
    this.cuesPanel.textContent = "טוען כתוביות…";
    // A slow re-render must not overwrite a faster later one.
    const token = (this._cueToken = Symbol("cues"));
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const cues = parseVtt(await response.text());
      if (token !== this._cueToken) return;
      const rows = cues.map((cue) =>
        el("li", {}, [
          el("span", { class: "cue-time", text: formatDuration(cue.start) }),
          el("span", { text: cue.text }),
        ])
      );
      this.cuesPanel.replaceChildren(el("ul", { class: "cue-list" }, rows));
      this.cues = cues;
      this.cueRows = rows;
      this._highlight(this.audio.currentTime);
    } catch (error) {
      if (token !== this._cueToken) return;
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
