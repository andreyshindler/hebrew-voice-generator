/* Recording a voiceover in the browser, instead of uploading one.

   Nothing here decodes or converts audio: MediaRecorder hands back whatever
   container the browser prefers - Opus in WebM on Chrome and Android, MP4 on
   Safari - and the server sniffs it like any other upload. Opus costs about
   60KB a minute, so even a ten-minute take is a few megabytes.

   The length is timed here rather than measured afterwards. A MediaRecorder
   blob frequently carries no duration in its metadata, so pointing a media
   element at it reports Infinity; but we started the clock, so we already know.

   Nothing is uploaded until the take is confirmed. A false start should cost
   no storage and no transcription quota, and transcription is billed by the
   second. */

import { $, el, formatDuration, toast } from "./ui.js";

/* How often the level meter and the clock redraw. Fast enough to look live,
   slow enough not to matter. */
const TICK_MS = 100;

/* Start warning this long before the cap, so a long take is not cut off
   without notice. */
const WARN_SECONDS = 30;

export class Recorder {
  constructor({ enabled, maxSeconds, onTake }) {
    this.enabled = Boolean(enabled);
    this.maxSeconds = maxSeconds || 0;
    this.onTake = onTake || (() => {});

    this.button = $("#record-btn");
    this.box = $("#record-box");
    this.clock = $("#record-clock");
    this.meter = $("#record-meter");
    this.preview = $("#record-preview");
    this.keep = $("#record-keep");
    this.again = $("#record-again");
    this.cancel = $("#record-cancel");
    this.stop = $("#record-stop");
    this.hint = $("#record-hint");

    this.stream = null;
    this.recorder = null;
    this.chunks = [];
    this.timer = null;
    this.startedAt = 0;
    this.seconds = 0;
    this.blob = null;
    this.audio = null;

    if (!this.button || !this.box) return;
    /* getUserMedia exists only in a secure context, so over plain HTTP - a
       bare IP, say - the button would be there and never work. */
    this.supported =
      Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) &&
      typeof MediaRecorder !== "undefined";
    this.button.hidden = !(this.enabled && this.supported);
    if (this.button.hidden) return;

    this.button.addEventListener("click", () => this.start());
    this.stop.addEventListener("click", () => this._stop());
    this.again.addEventListener("click", () => this._discard(true));
    this.cancel.addEventListener("click", () => this._discard(false));
    this.keep.addEventListener("click", () => this._keep());
  }

  async start() {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      /* Denied, dismissed, or no microphone at all. The browser's own message
         is in whatever language it was installed in, so say it ourselves. */
      toast(
        error && error.name === "NotAllowedError"
          ? "אין הרשאה למיקרופון. אפשרו אותה בהגדרות הדפדפן ונסו שוב"
          : "לא נמצא מיקרופון",
        "error"
      );
      return;
    }

    this.chunks = [];
    this.blob = null;
    this.recorder = new MediaRecorder(this.stream);
    this.recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) this.chunks.push(event.data);
    };
    this.recorder.onstop = () => this._settle();
    this.recorder.start();

    this.startedAt = Date.now();
    this._show("recording");
    this._listen();
    this.timer = window.setInterval(() => this._tick(), TICK_MS);
  }

  /* ------------------------------------------------------------ recording */

  /** Drive the level meter off the live stream, so a dead mic is obvious. */
  _listen() {
    try {
      const context = new (window.AudioContext || window.webkitAudioContext)();
      /* The context is built after `await getUserMedia`, by which point the
         click that started this no longer counts as a user gesture - so it
         opens suspended and never delivers a sample. The meter then sits at
         zero for a microphone that is working perfectly, which is precisely
         the lie it exists to prevent. */
      if (context.state === "suspended") context.resume().catch(() => {});
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      context.createMediaStreamSource(this.stream).connect(analyser);
      this.analyser = analyser;
      this.audioContext = context;
      this.samples = new Uint8Array(analyser.frequencyBinCount);
    } catch (error) {
      /* A meter is a nicety; losing it must not lose the recording. */
      this.analyser = null;
    }
  }

  _level() {
    if (!this.analyser) return 0;
    this.analyser.getByteTimeDomainData(this.samples);
    let peak = 0;
    for (const sample of this.samples) peak = Math.max(peak, Math.abs(sample - 128));
    /* 128 is full scale for this representation. The square root opens up the
       quiet end, where a voice actually sits. */
    return Math.min(1, Math.sqrt(peak / 128));
  }

  _tick() {
    this.seconds = (Date.now() - this.startedAt) / 1000;
    this.clock.textContent = formatDuration(this.seconds);
    /* Through the CSSOM: the app's CSP is style-src 'self' and refuses a style
       attribute outright, silently as far as layout is concerned. */
    this.meter.style.transform = `scaleX(${this._level().toFixed(3)})`;

    if (!this.maxSeconds) return;
    const left = this.maxSeconds - this.seconds;
    if (left <= 0) {
      this._stop();
      toast("ההקלטה נעצרה באורך המרבי", "info");
      return;
    }
    this.hint.textContent =
      left <= WARN_SECONDS ? `נותרו ${Math.ceil(left)} שניות` : "";
  }

  _stop() {
    if (this.recorder && this.recorder.state !== "inactive") this.recorder.stop();
  }

  /** Stop the recorder without letting it deliver the take.
   *
   * Releasing the stream makes the recorder stop by itself, so a cancel would
   * otherwise arrive at _settle and reopen the preview of the very take that
   * was just thrown away. Detaching the handlers first means the old recorder
   * cannot call back into us at all - which also matters for "record again",
   * where by the time it fired, this.recorder would be the new one.
   */
  _abandon() {
    const recorder = this.recorder;
    this.recorder = null;
    if (recorder) {
      recorder.onstop = null;
      recorder.ondataavailable = null;
      if (recorder.state !== "inactive") recorder.stop();
    }
    window.clearInterval(this.timer);
    this.timer = null;
    this.chunks = [];
  }

  /* -------------------------------------------------------------- the take */

  /** Called once the recorder has flushed everything it had. */
  _settle() {
    window.clearInterval(this.timer);
    this.timer = null;
    this._release();

    this.blob = new Blob(this.chunks, { type: this.recorder.mimeType });
    this.chunks = [];
    if (!this.blob.size) {
      toast("לא נקלט שמע", "error");
      this._show("idle");
      return;
    }

    /* A blob URL, which is why media-src carries blob: - see the CSP. */
    if (this.audio) URL.revokeObjectURL(this.audio.src);
    this.audio = el("audio", { controls: true, src: URL.createObjectURL(this.blob) });
    this.preview.replaceChildren(this.audio);
    this.clock.textContent = formatDuration(this.seconds);
    this._show("done");
  }

  /** Stop the microphone. The browser keeps its indicator lit until this. */
  _release() {
    if (this.stream) {
      for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
    }
    if (this.audioContext) {
      this.audioContext.close().catch(() => {});
      this.audioContext = null;
      this.analyser = null;
    }
  }

  _discard(retry) {
    this._abandon();
    this._release();
    if (this.audio) {
      URL.revokeObjectURL(this.audio.src);
      this.audio = null;
    }
    this.preview.replaceChildren();
    this.blob = null;
    this.seconds = 0;
    if (retry) this.start();
    else this._show("idle");
  }

  _keep() {
    if (!this.blob) return;
    /* Named for what it is, with the extension the container really uses -
       the server decides the stored name from the bytes, but a sensible
       filename is what the history ends up titled after. */
    const ext = (this.blob.type.includes("mp4") && "m4a") || "webm";
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    const file = new File([this.blob], `הקלטה-${stamp}.${ext}`, {
      type: this.blob.type,
    });
    const seconds = this.seconds;
    this._discard(false);
    this.onTake(file, seconds);
  }

  _show(state) {
    this.box.hidden = state === "idle";
    this.button.hidden = state !== "idle";
    this.stop.hidden = state !== "recording";
    /* Available while recording and after it: a take you have listened to and
       do not want is not the same as wanting another one, and "record again"
       is the only other way out of this panel. */
    this.cancel.hidden = state === "idle";
    this.keep.hidden = state !== "done";
    this.again.hidden = state !== "done";
    this.box.classList.toggle("is-recording", state === "recording");
    if (state !== "recording") this.hint.textContent = "";
    if (state === "recording") {
      this.clock.textContent = formatDuration(0);
      this.preview.replaceChildren();
    }
  }
}
