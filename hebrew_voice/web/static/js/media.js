/* Photos and clips to show behind the captions.

   Uploads go straight up as they are picked; the strip below the button is the
   running order of the finished video, left to right. */

import { api } from "./api.js";
import { $, el, formatBytes, icon, ICONS, toast } from "./ui.js";

export class MediaStrip {
  constructor({ onChange }) {
    this.onChange = onChange || (() => {});
    this.input = $("#media-input");
    this.strip = $("#media-strip");
    this.hint = $("#media-hint");
    this.items = [];
    this.busy = false;

    if (!this.input) return;
    this.input.addEventListener("change", () => this._pick());
  }

  /** Ids in running order, for the render request. */
  ids() {
    return this.items.map((item) => item.id);
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
      ...this.items.map((item, index) =>
        el("li", { class: "media-item", title: item.name || "" }, [
          /* Video gets a <video> rather than a poster frame: there is no
             thumbnailer in this image, and a first frame is enough to
             recognise a clip by. */
          item.kind === "video"
            ? el("video", { class: "media-thumb", src: item.url, muted: true, preload: "metadata" })
            : el("img", { class: "media-thumb", src: item.url, alt: "" }),
          el("span", { class: "media-index", dir: "ltr", text: String(index + 1) }),
          el(
            "button",
            {
              type: "button",
              class: "icon-btn btn-danger media-remove",
              title: "הסרה",
              "aria-label": "הסרה",
              onclick: () => this._remove(item),
            },
            [icon(ICONS.trash)]
          ),
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
