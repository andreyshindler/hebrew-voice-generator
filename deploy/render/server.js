/* The render sidecar.
 *
 * @hyperframes/producer ships its own HTTP server - queueing, progress, health
 * - so this only starts it. Writing our own around createRenderJob was the
 * first attempt and was a mistake: executeRenderJob takes the project
 * directory and output path as separate arguments and the frame rate as a
 * rational, none of which matches the shape the docs site describes.
 *
 * The contract the app codes against, read off the package rather than the
 * docs:
 *
 *   POST /render  { projectDir, entryFile, outputPath, fps, quality, format }
 *   GET  /health  -> { status: "ok", ... }
 *
 * projectDir is a real directory on the shared volume and entryFile is a name
 * inside it, which is what lets the composition reference its audio as a plain
 * relative filename. Nothing large crosses the socket in either direction.
 */

import { startServer } from "@hyperframes/producer/server";

const port = Number(process.env.PORT || 8080);

/* One at a time. The box is small and two Chromium instances seeking frames in
 * parallel finish later than the same two in sequence. The app has its own
 * limit; this is the backstop. */
const maxConcurrentRenders = Number(process.env.HF_MAX_CONCURRENT || 1);

await startServer({ port, maxConcurrentRenders });
console.log(`hyperframes renderer listening on ${port}`);
