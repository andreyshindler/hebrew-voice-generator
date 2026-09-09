/* The "make a video" corner of the result card.

   A render takes minutes, so the server queues it and hands back a row to
   poll. Everything here is that loop: ask, poll, then offer the file. */

import { api } from "./api.js";
import { $, el, formatBytes, toast } from "./ui.js";

/* Long enough not to hammer a box that is busy encoding, short enough that a
   quick render still feels responsive. */
const POLL_MS = 2500;
/* A render that has not finished by now is not going to; the worker has its
   own timeout and would have written a failure. */
const GIVE_UP_MS = 20 * 60 * 1000;

/* Mirrors RENDER_SIZES on the server, so an existing render can be matched
   to the selected shape without another round trip. */
const SIZES = {
  vertical: [1080, 1920],
  square: [1080, 1080],
  landscape: [1280, 720],
};

const LABELS = {
  queued: "בתור…",
  running: "מייצר וידאו…",
  failed: "היצירה נכשלה",
};

export class Renders {
  constructor({ enabled, maxSeconds }) {
    this.enabled = Boolean(enabled);
    this.maxSeconds = maxSeconds || 0;
    this.box = $("#render-box");
    this.note = $("#render-note");
    this.result = $("#render-result");
    this.button = $("#render-go");
    this.format = $("#render-format");
    this.size = $("#render-size");
    this.current = null;
    this.timer = null;

    if (!this.enabled) return;
    this.button.addEventListener("click", () => this._start());
    /* Either knob makes a different file, so anything shown for the previous
       combination no longer applies. */
    for (const control of [this.format, this.size]) {
      control.addEventListener("change", () => this._reset());
    }
  }

  /** Point the panel at a recording, or hide it when it can't be rendered. */
  show(generation) {
    this._stopPolling();
    this.current = generation;
    this.result.hidden = true;
    this.result.replaceChildren();
    this.note.textContent = "";

    if (!this.enabled || !generation) {
      this.box.hidden = true;
      return;
    }
    // Same gate as the caption controls: no word timings, nothing to lay out.
    const renderable =
      generation.can_regroup &&
      (!this.maxSeconds || generation.duration <= this.maxSeconds);
    this.box.hidden = !renderable;
    if (!renderable) return;

    this.button.disabled = false;
    /* An earlier render of this recording is worth surfacing - it cost real
       minutes and the server will hand the same file back anyway. */
    this._showExisting();
  }

  async _showExisting() {
    const generation = this.current;
    try {
      const { items } = await api.renders(generation.id);
      if (this.current !== generation) return;
      const live = items.find((r) => r.status === "queued" || r.status === "running");
      if (live) {
        this._follow(live);
        return;
      }
      const [width, height] = SIZES[this.size.value] || [];
      const done = items.find(
        (r) =>
          r.status === "done" &&
          r.format === this.format.value &&
          r.width === width &&
          r.height === height
      );
      if (done) this._offer(done);
    } catch (error) {
      /* Not worth a toast: the panel simply starts empty and the button works. */
    }
  }

  _reset() {
    this._stopPolling();
    this.result.hidden = true;
    this.result.replaceChildren();
    this.note.textContent = "";
    this.button.disabled = false;
    if (this.current) this._showExisting();
  }

  async _start() {
    if (!this.current) return;
    this.button.disabled = true;
    this.note.textContent = LABELS.queued;
    this.result.hidden = true;
    try {
      const render = await api.requestRender(this.current.id, {
        format: this.format.value,
        size: this.size.value,
      });
      if (render.status === "done") {
        // An identical render already existed, so there is nothing to wait for.
        this._offer(render);
        return;
      }
      this._follow(render);
    } catch (error) {
      this.button.disabled = false;
      this.note.textContent = "";
      toast(error.message, "error");
    }
  }

  _follow(render) {
    this.button.disabled = true;
    this.note.textContent = LABELS[render.status] || LABELS.running;
    const startedAt = Date.now();

    this.timer = window.setInterval(async () => {
      try {
        const latest = await api.render(render.id);
        if (latest.status === "done") {
          this._stopPolling();
          this._offer(latest);
          return;
        }
        if (latest.status === "failed") {
          this._stopPolling();
          this.button.disabled = false;
          this.note.textContent = "";
          toast(latest.error || LABELS.failed, "error");
          return;
        }
        this.note.textContent = LABELS[latest.status] || LABELS.running;
        if (Date.now() - startedAt > GIVE_UP_MS) {
          this._stopPolling();
          this.button.disabled = false;
          this.note.textContent = "היצירה נמשכת זמן רב. רעננו את הדף כדי לבדוק שוב";
        }
      } catch (error) {
        this._stopPolling();
        this.button.disabled = false;
        this.note.textContent = "";
        toast(error.message, "error");
      }
    }, POLL_MS);
  }

  _stopPolling() {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }

  _offer(render) {
    this.button.disabled = false;
    this.note.textContent = "";
    this.result.hidden = false;
    const label = render.format === "mp4" ? "הורדת וידאו" : "הורדת שכבה שקופה";
    this.result.replaceChildren(
      el("a", {
        class: "btn btn-primary",
        href: `${render.url}?download=1`,
        download: "",
        text: label,
      }),
      el("span", { class: "render-meta" }, [
        /* Without its own direction the surrounding RTL context reorders the
           two numbers and 1280x720 reads as 720x1280 - not a cosmetic
           problem, a wrong number. */
        el("span", { dir: "ltr", text: `${render.width}×${render.height}` }),
        el("span", { text: ` · ${formatBytes(render.video_bytes)}` }),
      ])
    );
  }
}
