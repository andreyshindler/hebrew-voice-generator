/* Photos and clips to show behind the captions.

   Uploads go straight up as they are picked; the strip below the button is the
   running order of the finished video, left to right. */

import { api } from "./api.js";
import { $, el, formatBytes, icon, ICONS, toast } from "./ui.js";

export class MediaStrip {
  constructor({ onChange, onTracks }) {
    this.onChange = onChange || (() => {});
    /* Audio uploads are music, not shots, so they go to the edit panel. */
    this.onTracks = onTracks || (() => {});
    this.input = $("#media-input");
    this.strip = $("#media-strip");
    this.hint = $("#media-hint");
    this.items = [];
    /* Relative hold per shot id. Proportions, not seconds: the server scales
       them to the voiceover, which is the only length that actually exists. */
    this.holds = {};
    this.busy = false;

    if (!this.input) return;
    this.input.addEventListener("change", () => this._pick());
  }

  /** Ids in running order, for the render request. */
  ids() {
    return this.shots().map((item) => item.id);
  }

  /** Only the shots - music is chosen separately and never appears here. */
  shots() {
    return this.items.filter((item) => item.kind !== "audio");
  }

  /** Audio uploads, offered as background music. */
  tracks() {
    return this.items.filter((item) => item.kind === "audio");
  }

  /** Relative hold per shot, in running order. Empty means an even split. */
  durations() {
    const shots = this.shots();
    return shots.some((item) => this.holds[item.id])
      ? shots.map((item) => this.holds[item.id] || 1)
      : [];
  }

  /** Move a shot earlier or later in the running order. */
  move(item, delta) {
    const from = this.items.indexOf(item);
    const shots = this.shots();
    const at = shots.indexOf(item);
    const to = at + delta;
    if (at < 0 || to < 0 || to >= shots.length) return;
    /* Reorder within the full list by swapping with the neighbouring *shot*,
       so an audio track sitting between them is not disturbed. */
    const target = this.items.indexOf(shots[to]);
    this.items.splice(from, 1);
    this.items.splice(target, 0, item);
    this._renderStrip();
    this.onChange();
  }

  async load() {
    if (!this.input) return;
    try {
      const { items, bytes_used, bytes_quota } = await api.media();
      /* The library is newest-first, which is the wrong way round for a
         running order: the first thing you added should be the first shot. */
      this.items = items.slice().reverse();
      this._renderStrip();
      this._renderHint(bytes_used, bytes_quota);
      this.onTracks(this.tracks());
    } catch (error) {
      /* An empty strip is a fine starting state; the button still works. */
    }
  }

  async _pick() {
    const files = Array.from(this.input.files || []);
    this.input.value = "";
    if (!files.length || this.busy) return;

    this.busy = true;
    for (const file of files) {
      try {
        /* Ask the browser how long a clip is before sending it: this image has
           no media tools, so the server cannot work it out for itself. It only
           ever affects layout, never a decision that matters. */
        const duration = await durationOf(file);
        const item = await api.uploadMedia(file, duration);
        this.items.push(item);
        this._renderStrip();
        this.onChange();
      } catch (error) {
        toast(`${file.name}: ${error.message}`, "error");
      }
    }
    this.busy = false;
    this.load();
  }

  async _remove(item) {
    try {
      await api.removeMedia(item.id);
      this.items = this.items.filter((other) => other.id !== item.id);
      this._renderStrip();
      this.onChange();
      this.load();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  _renderHint(used, quota) {
    if (!this.hint) return;
    this.hint.textContent = this.items.length
      ? `${this.items.length} קבצים · ${formatBytes(used)} מתוך ${formatBytes(quota)}`
      : "";
  }

  _renderStrip() {
    this.strip.replaceChildren(
      ...this.shots().map((item, index) =>
        /* A small card in normal flow rather than controls floated over the
           thumbnail: at this size they overlapped, and an invisible slider was
           swallowing clicks meant for the reorder buttons. */
        el("li", { class: "media-item", title: item.name || "" }, [
          /* Video gets a <video> rather than a poster frame: there is no
             thumbnailer in this image, and a first frame is enough to
             recognise a clip by. */
          item.kind === "video"
            ? el("video", {
                class: "media-thumb", src: item.url, muted: true, preload: "metadata",
              })
            : el("img", { class: "media-thumb", src: item.url, alt: "" }),

          el("div", { class: "media-bar" }, [
            el("button", {
              type: "button", class: "media-nudge", text: "‹",
              title: "מוקדם יותר", "aria-label": "מוקדם יותר",
              onclick: () => this.move(item, -1),
            }),
            el("span", { class: "media-index", dir: "ltr", text: String(index + 1) }),
            el("button", {
              type: "button", class: "media-nudge", text: "›",
              title: "מאוחר יותר", "aria-label": "מאוחר יותר",
              onclick: () => this.move(item, 1),
            }),
            el("button", {
              type: "button", class: "media-nudge media-remove", text: "×",
              title: "הסרה", "aria-label": "הסרה",
              onclick: () => this._remove(item),
            }),
          ]),

          /* How long this shot holds, relative to the others. The server
             scales them to the voiceover, so these are proportions. */
          el("input", {
            type: "range", class: "media-hold",
            min: "0.5", max: "3", step: "0.5",
            value: String(this.holds[item.id] || 1),
            title: "משך יחסי",
            "aria-label": "משך יחסי",
            onchange: (event) => {
              this.holds[item.id] = Number(event.target.value);
              this.onChange();
            },
          }),
        ])
      )
    );
  }
}

/** Read a clip's length in the browser. Images resolve to 0. */
function durationOf(file) {
  if (!file.type.startsWith("video/")) return Promise.resolve(0);
  return new Promise((resolve) => {
    const probe = document.createElement("video");
    probe.preload = "metadata";
    const src = URL.createObjectURL(file);
    const done = (value) => {
      URL.revokeObjectURL(src);
      resolve(value);
    };
    probe.onloadedmetadata = () => done(Number.isFinite(probe.duration) ? probe.duration : 0);
    probe.onerror = () => done(0);
    probe.src = src;
  });
}
