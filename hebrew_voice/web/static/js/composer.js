/* The left-hand pane: text input, voice choice, sliders, advanced panel,
   and the live "this is what will be spoken" preview. */

import { api } from "./api.js";
import { $, debounce, el, formatNumber, formatDuration, toast } from "./ui.js";

const OPTIONS_KEY = "hv:options";
const DRAFT_KEY = "hv:draft";

const DEFAULTS = {
  voice: null,
  rate: 0,
  pitch: 0,
  volume: 0,
  keep_niqqud: false,
  expand_symbols: true,
  expand_abbreviations: true,
  expand_acronyms: true,
  subtitles: true,
  words_per_cue: 7,
};

const CLEANUP_FLAGS = [
  "keep_niqqud",
  "expand_symbols",
  "expand_abbreviations",
  "expand_acronyms",
];

export class Composer {
  constructor({ voices, maxChars }) {
    this.voices = voices;
    this.maxChars = maxChars;
    this.state = { ...DEFAULTS, ...this._loadOptions() };
    if (!this.state.voice) {
      const fallback = voices.find((v) => v.default) || voices[0];
      this.state.voice = fallback ? fallback.id : null;
    }
    this.previewAbort = null;

    this.textarea = $("#text");
    this.charCount = $("#char-count");
    this.previewText = $("#preview-text");
    this.previewEst = $("#preview-est");

    this._renderVoices();
    this._restoreControls();
    this._wireText();
    this._wireSliders();
    this._wireToggles();
    this._wireFileInput();
    this._updateCharCount();
  }

  /* ------------------------------------------------------------ state */

  _loadOptions() {
    try {
      return JSON.parse(localStorage.getItem(OPTIONS_KEY) || "{}");
    } catch (err) {
      return {};
    }
  }

  _saveOptions() {
    try {
      localStorage.setItem(OPTIONS_KEY, JSON.stringify(this.state));
    } catch (err) {
      /* private mode - settings just won't persist */
    }
  }

  get text() {
    return this.textarea.value;
  }

  set text(value) {
    this.textarea.value = value;
    this._updateCharCount();
    this.schedulePreview();
  }

  /** The body of a /api/synthesize request. */
  payload() {
    return {
      text: this.text,
      voice: this.state.voice,
      rate: this.state.rate,
      pitch: this.state.pitch,
      volume: this.state.volume,
      subtitles: this.state.subtitles,
      words_per_cue: this.state.words_per_cue,
      keep_niqqud: this.state.keep_niqqud,
      expand_symbols: this.state.expand_symbols,
      expand_abbreviations: this.state.expand_abbreviations,
      expand_acronyms: this.state.expand_acronyms,
    };
  }

  /** Reload a past generation's text and settings into the form. */
  load(generation) {
    this.state.voice = generation.voice;
    this.state.rate = generation.rate;
    this.state.pitch = generation.pitch;
    this.state.volume = generation.volume;
    if (generation.words_per_cue) this.state.words_per_cue = generation.words_per_cue;
    Object.assign(this.state, generation.options || {});
    this._restoreControls();
    this._renderVoices();
    this._saveOptions();
    this.text = generation.text || "";
    this.textarea.focus();
  }

  /* --------------------------------------------------------- rendering */

  _renderVoices() {
    const host = $("#voice-picker");
    host.replaceChildren(
      ...this.voices.map((voice) => {
        const selected = voice.id === this.state.voice;
        const option = el(
          "label",
          {
            class: `voice-option${selected ? " is-selected" : ""}`,
            role: "radio",
            "aria-checked": String(selected),
          },
          [
            el("input", {
              type: "radio",
              name: "voice",
              value: voice.id,
              checked: selected,
              onchange: () => {
                this.state.voice = voice.id;
                this._saveOptions();
                this._renderVoices();
              },
            }),
            el("span", {
              class: "voice-avatar",
              text: voice.gender === "male" ? "♂" : "♀",
            }),
            el("span", {}, [
              el("span", { class: "voice-name", text: voice.label }),
              el("span", { class: "voice-desc", text: voice.description }),
            ]),
          ]
        );
        return option;
      })
    );
  }

  _restoreControls() {
    for (const key of ["rate", "pitch", "volume"]) {
      const input = $(`#${key}`);
      input.value = this.state[key];
      this._renderSliderValue(key);
    }
    for (const key of CLEANUP_FLAGS) {
      $(`#${key}`).checked = Boolean(this.state[key]);
    }
    $("#subtitles").checked = Boolean(this.state.subtitles);
    $("#words_per_cue").value = String(this.state.words_per_cue);
    this._syncSubtitleControls();
  }

  /** The density only means anything if subtitles are being written at all. */
  _syncSubtitleControls() {
    $("#density-row").hidden = !this.state.subtitles;
  }

  _renderSliderValue(key) {
    const value = this.state[key];
    const unit = key === "pitch" ? "Hz" : "%";
    const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
    $(`#${key}-out`).textContent = `${sign}${Math.abs(value)}${unit}`;
  }

  _updateCharCount() {
    const length = this.text.trim().length;
    this.charCount.textContent = `${formatNumber(length)} / ${formatNumber(this.maxChars)} תווים`;
    this.charCount.classList.toggle("is-warn", length > this.maxChars * 0.9 && length <= this.maxChars);
    this.charCount.classList.toggle("is-over", length > this.maxChars);
  }

  /* ------------------------------------------------------------ wiring */

  _wireText() {
    const onInput = () => {
      this._updateCharCount();
      this._saveDraft();
      this.schedulePreview();
    };
    this.textarea.addEventListener("input", onInput);

    const saved = (() => {
      try {
        return localStorage.getItem(DRAFT_KEY) || "";
      } catch (err) {
        return "";
      }
    })();
    if (saved) {
      this.textarea.value = saved;
      this._updateCharCount();
    }

    $("#clear-btn").addEventListener("click", () => {
      this.text = "";
      this.textarea.focus();
    });
  }

  _saveDraft = debounce(() => {
    try {
      localStorage.setItem(DRAFT_KEY, this.text);
    } catch (err) {
      /* ignore */
    }
  }, 500);

  _wireSliders() {
    for (const key of ["rate", "pitch", "volume"]) {
      $(`#${key}`).addEventListener("input", (event) => {
        this.state[key] = Number(event.target.value);
        this._renderSliderValue(key);
        this._saveOptions();
        if (key === "rate") this.schedulePreview();
      });
    }
    $("#reset-sliders").addEventListener("click", () => {
      for (const key of ["rate", "pitch", "volume"]) {
        this.state[key] = 0;
        $(`#${key}`).value = 0;
        this._renderSliderValue(key);
      }
      this._saveOptions();
      this.schedulePreview();
    });
  }

  _wireToggles() {
    for (const key of CLEANUP_FLAGS) {
      $(`#${key}`).addEventListener("change", (event) => {
        this.state[key] = event.target.checked;
        this._saveOptions();
        this.schedulePreview();
      });
    }
    $("#subtitles").addEventListener("change", (event) => {
      this.state.subtitles = event.target.checked;
      this._syncSubtitleControls();
      this._saveOptions();
    });
    $("#words_per_cue").addEventListener("change", (event) => {
      this.state.words_per_cue = Number(event.target.value);
      this._saveOptions();
    });
  }

  /* -------------------------------------------------------- .txt upload */

  _wireFileInput() {
    const input = $("#file-input");
    const zone = $("#drop-zone");

    $("#upload-btn").addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      if (input.files && input.files[0]) this._readFile(input.files[0]);
      input.value = "";
    });

    // Read the file in the browser and drop it into the textarea: same effect
    // as `--file script.txt`, still editable, and no upload endpoint to guard.
    ["dragenter", "dragover"].forEach((name) =>
      zone.addEventListener(name, (event) => {
        event.preventDefault();
        zone.classList.add("is-dragging");
      })
    );
    ["dragleave", "drop"].forEach((name) =>
      zone.addEventListener(name, (event) => {
        event.preventDefault();
        if (name === "dragleave" && zone.contains(event.relatedTarget)) return;
        zone.classList.remove("is-dragging");
      })
    );
    zone.addEventListener("drop", (event) => {
      const file = event.dataTransfer && event.dataTransfer.files[0];
      if (file) this._readFile(file);
    });
  }

  _readFile(file) {
    const limit = 2 * 1024 * 1024;
    if (file.size > limit) {
      toast("הקובץ גדול מדי (מקסימום 2MB)", "error");
      return;
    }
    if (file.type && !file.type.startsWith("text/")) {
      toast("אפשר להעלות קובצי טקסט בלבד", "error");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      this.text = String(reader.result || "");
      toast(`נטען הקובץ ${file.name}`, "ok");
    };
    reader.onerror = () => toast("קריאת הקובץ נכשלה", "error");
    reader.readAsText(file, "utf-8");
  }

  /* ------------------------------------------------------------ preview */

  schedulePreview = debounce(() => this.refreshPreview(), 300);

  async refreshPreview() {
    if (!$("#advanced").open) return; // don't spend requests on a closed panel
    const text = this.text;
    if (!text.trim()) {
      this.previewText.textContent = "";
      this.previewEst.textContent = "";
      return;
    }
    if (this.previewAbort) this.previewAbort.abort();
    this.previewAbort = new AbortController();
    try {
      const result = await api.preview(
        {
          text: text.slice(0, this.maxChars),
          rate: this.state.rate,
          keep_niqqud: this.state.keep_niqqud,
          expand_symbols: this.state.expand_symbols,
          expand_abbreviations: this.state.expand_abbreviations,
          expand_acronyms: this.state.expand_acronyms,
        },
        this.previewAbort.signal
      );
      this.previewText.textContent = result.prepared;
      this.previewEst.textContent = `כ‑${formatDuration(result.estimated_seconds)} דקות`;
    } catch (error) {
      if (error.name !== "AbortError") this.previewEst.textContent = "";
    }
  }
}
