/* Correcting what the recogniser heard.

   Hebrew speech recognition gets proper nouns, numbers and loanwords wrong,
   and every mistake ends up burned into the video. So the words are editable
   before they become subtitles.

   The server re-times them by aligning the corrected words against the
   recognised ones: a word left alone keeps the moment it was actually said at,
   and only what changed is re-spread. Swapping a name is exact; rewriting a
   whole sentence is a guess, which the panel says out loud. */

import { api } from "./api.js";
import { $, toast } from "./ui.js";

export class TranscriptEditor {
  constructor({ onSaved }) {
    this.onSaved = onSaved || (() => {});
    this.box = $("#transcript-box");
    this.field = $("#transcript-text");
    this.save = $("#transcript-save");
    this.reset = $("#transcript-reset");
    this.status = $("#transcript-status");
    this.current = null;
    this.original = "";

    if (!this.box) return;
    this.field.addEventListener("input", () => this._dirty());
    this.save.addEventListener("click", () => this._save());
    this.reset.addEventListener("click", () => {
      this.field.value = this.original;
      this._dirty();
    });
  }

  /** Show the panel for a transcribed recording, and hide it for anything else. */
  show(generation) {
    if (!this.box) return;
    /* Only a transcript can be corrected. A synthesised recording's text is
       its input - editing it here would leave the words disagreeing with the
       audio, with no way back. */
    const editable = generation && generation.source === "transcription";
    this.box.hidden = !editable;
    if (!editable) {
      this.current = null;
      return;
    }
    this.current = generation.id;
    this.original = generation.text || "";
    this.field.value = this.original;
    this._dirty();
  }

  _dirty() {
    const changed = this.field.value.trim() !== this.original.trim();
    this.save.disabled = !changed || !this.field.value.trim();
    this.reset.hidden = !changed;
    if (this.status) this.status.textContent = "";
  }

  async _save() {
    const text = this.field.value.trim();
    if (!this.current || !text) return;
    this.save.disabled = true;
    if (this.status) this.status.textContent = "שומר…";
    try {
      const generation = await api.editTranscript(this.current, text);
      this.original = generation.text || text;
      this._dirty();
      toast("הכתוביות עודכנו", "ok");
      /* The cue list, the downloads and the video editor all read from the
         recording, and every one of them just changed. */
      this.onSaved(generation);
    } catch (error) {
      if (this.status) this.status.textContent = "";
      this.save.disabled = false;
      toast(error.message, "error");
    }
  }
}
