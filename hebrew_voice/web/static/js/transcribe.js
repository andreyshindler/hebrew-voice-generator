/* Subtitles from a recording instead of from typing.

   The upload goes through the same media endpoint as photos and clips - a
   recording is just another file on the volume until someone asks for it to be
   transcribed. After that it is the render loop again: ask, poll, then open
   what came back. */

import { api } from "./api.js";
import { $, toast } from "./ui.js";

/* Same cadence as a render. Transcription is usually faster, but the poll is
   cheap and the wait is the same shape. */
const POLL_MS = 2500;
/* Longer than the server's own timeout, so a job that really is stuck is
   reported by the server rather than guessed at here. */
const GIVE_UP_MS = 10 * 60 * 1000;

export class Transcriber {
  constructor({ enabled, maxSeconds, onDone }) {
    this.enabled = Boolean(enabled);
    this.maxSeconds = maxSeconds || 0;
    this.onDone = onDone || (() => {});
    this.button = $("#transcribe-btn");
    this.input = $("#transcribe-input");
    this.status = $("#transcribe-status");
    this.timer = null;

    if (!this.button || !this.input) return;
    /* Nothing to offer without a provider, so the control is not shown at
       all rather than shown and refused. */
    this.button.hidden = !this.enabled;
    if (!this.enabled) return;

    this.button.addEventListener("click", () => this.input.click());
    this.input.addEventListener("change", () => this._pick());
  }

  _say(message) {
    if (this.status) this.status.textContent = message || "";
  }

  _busy(busy) {
    this.button.disabled = busy;
    this.input.disabled = busy;
  }

  async _pick() {
    const file = (this.input.files || [])[0];
    this.input.value = "";
    if (!file) return;
    /* Measure here for the same reason the timeline does: this image has no
       media tools, so the server cannot work out how long a clip is. It only
       sizes the quota reservation - the provider returns the real duration and
       the charge is corrected then. */
    await this.submit(file, await durationOf(file));
  }

  /** Upload something and transcribe it. The recorder comes in here too.
   *
   * A recording arrives with its length already known - it was timed as it was
   * made - so the caller passes it rather than making this measure a blob it
   * would have to decode first.
   */
  async submit(file, seconds) {
    this._busy(true);
    try {
      if (this.maxSeconds && seconds > this.maxSeconds) {
        throw new Error(
          `ההקלטה ארוכה מ‑${Math.round(this.maxSeconds / 60)} דקות`
        );
      }
      this._say("מעלה…");
      const media = await api.uploadMedia(file, seconds);
      this._say("מתמלל…");
      const job = await api.transcribe(media.id);
      await this._poll(job);
    } catch (error) {
      this._say("");
      toast(error.message, "error");
      this._busy(false);
    }
  }

  _poll(job) {
    return new Promise((resolve) => {
      const started = Date.now();
      const stop = (message, kind) => {
        window.clearInterval(this.timer);
        this.timer = null;
        this._say("");
        this._busy(false);
        if (message) toast(message, kind);
        resolve();
      };

      this.timer = window.setInterval(async () => {
        if (Date.now() - started > GIVE_UP_MS) {
          stop("התמלול נמשך זמן רב מדי", "error");
          return;
        }
        let current;
        try {
          current = await api.transcription(job.id);
        } catch (error) {
          stop(error.message, "error");
          return;
        }
        if (current.status === "failed") {
          stop(current.error || "התמלול נכשל", "error");
          return;
        }
        if (current.generation_id) {
          stop("", null);
          this.onDone(current.generation_id);
        }
      }, POLL_MS);
    });
  }
}

/** Read a recording's length in the browser. Unreadable files resolve to 0. */
function durationOf(file) {
  return new Promise((resolve) => {
    /* A <video> element reads the duration of bare audio too, which saves
       picking an element per container. */
    const probe = document.createElement("video");
    probe.preload = "metadata";
    const src = URL.createObjectURL(file);
    const done = (value) => {
      URL.revokeObjectURL(src);
      resolve(value);
    };
    probe.onloadedmetadata = () =>
      done(Number.isFinite(probe.duration) ? probe.duration : 0);
    probe.onerror = () => done(0);
    probe.src = src;
  });
}
