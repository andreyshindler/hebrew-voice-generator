/* The generation history list: paging, replay, reload-into-composer, delete. */

import { api } from "./api.js";
import { $, ICONS, el, formatDuration, icon, relativeTime, toast } from "./ui.js";

const PAGE_SIZE = 15;

export class History {
  constructor({ voices, onPlay, onRestore }) {
    this.voiceLabels = Object.fromEntries(voices.map((v) => [v.id, v.label]));
    this.onPlay = onPlay;
    this.onRestore = onRestore;
    this.items = [];
    this.nextBefore = null;
    this.playingId = null;

    this.list = $("#history-list");
    this.empty = $("#history-empty");
    this.more = $("#history-more");
    this.more.addEventListener("click", () => this.load({ append: true }));
  }

  async load({ append = false } = {}) {
    try {
      const page = await api.history({
        limit: PAGE_SIZE,
        before: append ? this.nextBefore : undefined,
      });
      this.items = append ? this.items.concat(page.items) : page.items;
      this.nextBefore = page.next_before;
      this.more.hidden = !page.has_more;
      this.render();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  prepend(generation) {
    this.items.unshift(generation);
    this.render();
  }

  markPlaying(id) {
    this.playingId = id;
    this.render();
  }

  render() {
    this.empty.hidden = this.items.length > 0;
    this.list.replaceChildren(...this.items.map((item) => this._row(item)));
  }

  _row(item) {
    const action = (label, paths, handler, extraClass = "") =>
      el(
        "button",
        {
          type: "button",
          class: `icon-btn ${extraClass}`,
          title: label,
          "aria-label": label,
          onclick: handler,
        },
        [icon(paths, { fill: paths === ICONS.play ? "currentColor" : "none" })]
      );

    return el(
      "li",
      {
        class: `history-item${item.id === this.playingId ? " is-playing" : ""}`,
        dataset: { id: item.id },
      },
      [
        el("div", {}, [
          el("div", { class: "history-title", text: item.title, title: item.title }),
          el("div", { class: "history-meta" }, [
            el("span", { class: "voice-badge", text: this.voiceLabels[item.voice] || item.voice }),
            el("span", { text: formatDuration(item.duration) }),
            el("span", { text: relativeTime(item.created_at) }),
          ]),
        ]),
        el("div", { class: "history-actions" }, [
          action("השמעה", ICONS.play, () => this.onPlay(item)),
          el(
            "a",
            {
              class: "icon-btn",
              href: `${item.urls.audio}?download=1`,
              download: "",
              title: "הורדה",
              "aria-label": "הורדה",
            },
            [icon(ICONS.download)]
          ),
          action("טעינת ההגדרות מחדש", ICONS.restore, () => this._restore(item)),
          action("מחיקה", ICONS.trash, () => this._remove(item), "btn-danger"),
        ]),
      ]
    );
  }

  async _restore(item) {
    try {
      this.onRestore(await api.generation(item.id));
      toast("ההגדרות נטענו מחדש", "ok");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async _remove(item) {
    if (!window.confirm(`למחוק את "${item.title}"?`)) return;
    try {
      await api.remove(item.id);
      this.items = this.items.filter((other) => other.id !== item.id);
      this.render();
      toast("הפריט נמחק", "ok");
    } catch (error) {
      toast(error.message, "error");
    }
  }
}
