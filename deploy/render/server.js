/* The render sidecar.
 *
 * @hyperframes/producer ships an HTTP server mode: POST /render with a
 * RenderConfig. We wrap it only to add a health endpoint, because the deploy
 * script and compose both need one to know the container is actually up.
 *
 * Input and output are paths on the shared /data volume, not payloads - a
 * rendered video is far too big to push through a request body twice.
 */

const http = require("node:http");
const { createRenderJob, executeRenderJob } = require("@hyperframes/producer");

const PORT = Number(process.env.PORT || 8080);

/* One render at a time. The box is small, and two Chromium instances seeking
 * frames in parallel is slower than doing them in turn. The app enforces its
 * own limit too; this is the backstop if anything else ever calls in. */
let busy = false;

function send(response, status, body) {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
  });
  response.end(payload);
}

async function readJson(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    // A RenderConfig is a handful of fields; anything larger is not one.
    if (size > 1 << 20) throw new Error("request body too large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/healthz") {
    return send(response, 200, { ok: true, busy });
  }
  if (request.method !== "POST" || request.url !== "/render") {
    return send(response, 404, { error: "not found" });
  }
  if (busy) {
    return send(response, 503, { error: "a render is already running" });
  }

  busy = true;
  const started = Date.now();
  try {
    const config = await readJson(request);
    for (const key of ["inputPath", "outputPath"]) {
      if (typeof config[key] !== "string" || !config[key]) {
        return send(response, 400, { error: `${key} is required` });
      }
    }
    const job = createRenderJob({
      inputPath: config.inputPath,
      outputPath: config.outputPath,
      width: config.width ?? 1280,
      height: config.height ?? 720,
      fps: config.fps ?? 30,
      quality: config.quality ?? "standard",
      format: config.format ?? "mp4",
    });
    const result = await executeRenderJob(job);
    const seconds = ((Date.now() - started) / 1000).toFixed(1);
    console.log(`rendered ${config.format} in ${seconds}s -> ${result.outputPath}`);
    send(response, 200, { outputPath: result.outputPath, seconds: Number(seconds) });
  } catch (error) {
    console.error("render failed:", error);
    // The app surfaces this message to the user, so keep it short and real.
    send(response, 500, { error: String((error && error.message) || error) });
  } finally {
    busy = false;
  }
});

/* A render is minutes long; the default two-minute socket timeout would cut
 * the connection while the work is still going. The app applies its own
 * overall timeout. */
server.requestTimeout = 0;
server.headersTimeout = 0;
server.timeout = 0;

server.listen(PORT, () => console.log(`hyperframes renderer listening on ${PORT}`));
