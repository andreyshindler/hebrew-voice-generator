/* The timeline: which shots play, in what order, and for how long.

   Each block's width is its share of the voiceover, so the cut is readable at
   a glance. Dragging the edge between two blocks moves time from one to the
   other - the total is fixed by the audio, which is the only length that
   really exists. */

import { api } from "./api.js";
import { $, el, formatBytes, toast } from "./ui.js";

/* Below this a block has no room for its label, so it stops shrinking and its
   neighbour gives way instead. */
const MIN_SHARE = 0.04;

export class Timeline {
  constructor({ onChange, onTracks }) {
    this.onChange = onChange || (() => {});
    /* Audio uploads are music, not shots, so they go to the edit panel. */
    this.onTracks = onTracks || (() => {});
    this.input = $("#media-input");
    this.track = $("#tl-track");
    this.ruler = $("#tl-ruler");
    this.empty = $("#tl-empty");
    this.hint = $("#media-hint");
    this.items = [];
    /* Relative hold per shot id. Proportions, not seconds: the server scales
       them to the voiceover. */
    this.holds = {};
    this.duration = 0;
    this.busy = false;

    if (!this.input) return;
    this.input.addEventListener("change", () => this._pick());
  }

  /** The voiceover's length, so the ruler and labels can show real seconds. */
  setDuration(seconds) {
    this.duration = seconds || 0;
    this._render();
  }

  ids() {
    return this.shots().map((item) => item.id);
  }

  shots() {
    return this.items.filter((item) => item.kind !== "audio");
  }

  tracks() {
    return this.items.filter((item) => item.kind === "audio");
  }

  durations() {
    const shots = this.shots();
    return shots.some((item) => this.holds[item.id])
      ? shots.map((item) => this.holds[item.id] || 1)
      : [];
  }

  async load() {
    if (!this.input) return;
    try {
      const { items, bytes_used, bytes_quota } = await api.media();
      /* The library is newest-first, which is the wrong way round for a
         running order: the first thing you added should be the first shot. */
      this.items = items.slice().reverse();
      this._render();
      this._renderHint(bytes_used, bytes_quota);
      this.onTracks(this.tracks());
    } catch (error) {
      /* An empty timeline is a fine starting state; the button still works. */
    }
  }

  move(item, delta) {
    const shots = this.shots();
    const at = shots.indexOf(item);
    const to = at + delta;
    if (at < 0 || to < 0 || to >= shots.length) return;
    /* Reorder within the full list by swapping with the neighbouring *shot*,
       so an audio track sitting between them is not disturbed. */
    const from = this.items.indexOf(item);
    const target = this.items.indexOf(shots[to]);
    this.items.splice(from, 1);
    this.items.splice(target, 0, item);
    this._render();
    this.onChange();
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
        this._render();
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
      delete this.holds[item.id];
      this._render();
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

  /** Each shot's share of the whole, in running order. */
  _shares() {
    const weights = this.shots().map((item) => this.holds[item.id] || 1);
    const total = weights.reduce((sum, value) => sum + value, 0) || 1;
    return weights.map((value) => value / total);
  }

  _renderRuler() {
    if (!this.ruler) return;
    this.ruler.replaceChildren();
    if (!this.duration) return;
    /* A tick a second while that stays legible, then coarser. */
    const step = this.duration <= 20 ? 1 : this.duration <= 60 ? 5 : 10;
    for (let second = 0; second <= this.duration + 0.001; second += step) {
      /* The number is LTR, but the tick itself must stay RTL: ``dir`` on the
         positioned element would flip its own inline start and send the ruler
         the opposite way to the track. */
      const tick = el("span", { class: "tl-tick" }, [
        el("bdi", { dir: "ltr", text: `${second}s` }),
      ]);
      /* Assigned through the CSSOM, not a style attribute: the app's CSP is
         style-src 'self', which refuses inline styles outright - silently, as
         far as layout is concerned. */
      tick.style.insetInlineStart = `${(second / this.duration) * 100}%`;
      this.ruler.append(tick);
    }
  }

  _render() {
    if (!this.track) return;
    this._renderRuler();
    const shots = this.shots();
    if (this.empty) this.empty.hidden = shots.length > 0;

    const shares = this._shares();
    const blocks = [];
    shots.forEach((item, index) => {
      blocks.push(this._block(item, index, shares[index]));
      /* A grip between neighbours, to move time from one into the other. */
      if (index < shots.length - 1) {
        blocks.push(
          el("div", {
            class: "tl-grip",
            role: "separator",
            tabindex: "0",
            "aria-label": "שינוי משך",
            onpointerdown: (event) => this._drag(event, index),
            onkeydown: (event) => this._key(event, index),
          })
        );
      }
    });
    this.track.replaceChildren(...blocks);
  }

  _block(item, index, share) {
    const seconds = this.duration ? this.duration * share : 0;
    const block = el(
      "div",
      {
        class: "tl-block",
        title: item.name || "",
        dataset: { id: item.id },
      },
      [
        item.kind === "video"
          ? el("video", { class: "tl-thumb", src: item.url, muted: true, preload: "metadata" })
          : el("img", { class: "tl-thumb", src: item.url, alt: "" }),
        el("div", { class: "tl-meta" }, [
          el("span", { class: "tl-index", dir: "ltr", text: String(index + 1) }),
          el("span", {
            class: "tl-seconds",
            dir: "ltr",
            text: seconds ? `${seconds.toFixed(1)}s` : "",
          }),
        ]),
        el("div", { class: "tl-tools" }, [
          el("button", {
            type: "button", class: "tl-btn", text: "‹",
            title: "מוקדם יותר", "aria-label": "מוקדם יותר",
            onclick: () => this.move(item, -1),
          }),
          el("button", {
            type: "button", class: "tl-btn", text: "›",
            title: "מאוחר יותר", "aria-label": "מאוחר יותר",
            onclick: () => this.move(item, 1),
          }),
          el("button", {
            type: "button", class: "tl-btn tl-del", text: "×",
            title: "הסרה", "aria-label": "הסרה",
            onclick: () => this._remove(item),
          }),
        ]),
      ]
    );
    /* Same reason as the ruler: a style attribute would be refused by the CSP,
       and the block would silently fall back to its minimum width. */
    block.style.flex = `${Math.max(share, MIN_SHARE)} 1 0`;
    return block;
  }

  /** Drag the boundary after shot ``index``, moving time between neighbours. */
  _drag(event, index) {
    const boundary = this._boundary(index);
    if (!boundary) return;
    event.preventDefault();

    const width = this.track.getBoundingClientRect().width || 1;
    const startX = event.clientX;
    /* The page is RTL, so dragging right *shrinks* the earlier shot. */
    const rtl = getComputedStyle(this.track).direction === "rtl";

    const onMove = (moveEvent) => {
      const travelled = (moveEvent.clientX - startX) / width;
      this._shift(boundary, travelled * boundary.total * (rtl ? -1 : 1));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      this.onChange();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  /** The same boundary by keyboard: a 2%-of-the-whole nudge per press. */
  _key(event, index) {
    const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!step) return;
    const boundary = this._boundary(index);
    if (!boundary) return;
    event.preventDefault();
    const rtl = getComputedStyle(this.track).direction === "rtl";
    this._shift(boundary, step * 0.02 * boundary.total * (rtl ? -1 : 1));
    /* _shift redraws the track, so the grip holding focus no longer exists.
       Hand it to the one that replaced it, or the next press goes nowhere. */
    const grips = this.track.querySelectorAll(".tl-grip");
    if (grips[index]) grips[index].focus();
    this.onChange();
  }

  /** What a gesture on the boundary after ``index`` is working with. */
  _boundary(index) {
    const shots = this.shots();
    const left = shots[index];
    const right = shots[index + 1];
    if (!left || !right) return null;
    const weights = shots.map((item) => this.holds[item.id] || 1);
    const total = weights.reduce((sum, value) => sum + value, 0) || 1;
    return {
      left,
      right,
      total,
      startLeft: weights[index],
      pair: weights[index] + weights[index + 1],
    };
  }

  /* ``by`` is measured from where the gesture started, not from the last
     frame, so a drag that returns to its origin restores the original split. */
  _shift(boundary, by) {
    const floor = MIN_SHARE * boundary.total;
    const next = Math.min(
      boundary.pair - floor,
      Math.max(floor, boundary.startLeft + by)
    );
    this.holds[boundary.left.id] = next;
    this.holds[boundary.right.id] = boundary.pair - next;
    this._render();
  }
}

/** Read a clip's length in the browser. Images and audio resolve to 0. */
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
