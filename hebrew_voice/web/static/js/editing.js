/* The edit panel: caption styling, motion, and background music.

   Everything here is presentation, and every value is clamped again on the
   server - this is the convenient way to set them, not the enforcement. */

import { $ } from "./ui.js";

export class EditPanel {
  constructor({ onChange }) {
    this.onChange = onChange || (() => {});
    this.panel = $("#edit-panel");
    if (!this.panel) return;

    this.controls = {
      scale: $("#cap-scale"),
      position: $("#cap-position"),
      color: $("#cap-color"),
      karaoke: $("#cap-karaoke"),
      box: $("#cap-box"),
      zoom: $("#motion-zoom"),
      fade: $("#motion-fade"),
      music: $("#music-select"),
      volume: $("#music-volume"),
    };

    for (const control of Object.values(this.controls)) {
      /* `change` rather than `input`: a slider fires input per pixel, and each
         one would re-check whether a matching render already exists. */
      control.addEventListener("change", () => this.onChange());
    }
  }

  /** Offer the account's audio uploads as music, keeping the selection. */
  setTracks(tracks) {
    const select = this.controls && this.controls.music;
    if (!select) return;
    const chosen = select.value;
    select.replaceChildren(
      ...[{ id: "", name: "ללא" }, ...tracks].map((track) => {
        const option = document.createElement("option");
        option.value = track.id;
        option.textContent = track.name || track.id.slice(0, 8);
        return option;
      })
    );
    /* A track deleted while selected falls back to none rather than sending an
       id the server will reject. */
    select.value = tracks.some((t) => t.id === chosen) ? chosen : "";
  }

  /** The plan for a render request, or defaults when the panel is absent. */
  plan(durations) {
    if (!this.panel) return { durations: durations || [] };
    const c = this.controls;
    const music = c.music.value
      ? { id: c.music.value, volume: Number(c.volume.value) }
      : null;
    return {
      durations: durations || [],
      caption: {
        scale: Number(c.scale.value),
        position: c.position.value,
        color: c.color.value,
        karaoke: c.karaoke.checked,
        box: c.box.checked,
      },
      motion: { zoom: c.zoom.checked, fade: c.fade.checked },
      music,
    };
  }
}
