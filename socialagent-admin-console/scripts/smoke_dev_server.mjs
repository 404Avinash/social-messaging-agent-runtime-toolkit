// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Smoke test for the Vite dev server behind `npm run dev` / `make ui-dev`.
//
// `npm run build` and vitest never run Vite's dependency optimizer, so a break
// that only affects the dev server passes CI: an esbuild the optimizer could
// not use once crashed `make ui-dev` on startup while every check stayed green.
// This starts the dev server in-process, requests the entry module and every
// pre-bundled dependency it imports, and fails on any error Vite logs.

import { createLogger, createServer } from "vite";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const TIMEOUT_MS = 120_000;

function fail(message) {
  console.error(`smoke:dev FAILED: ${message}`);
  process.exit(1);
}

process.on("unhandledRejection", (err) => fail(`unhandled rejection: ${err?.stack ?? err}`));
process.on("uncaughtException", (err) => fail(`uncaught exception: ${err?.stack ?? err}`));
setTimeout(() => fail(`no result after ${TIMEOUT_MS / 1000}s`), TIMEOUT_MS).unref();

const errors = [];
const logger = createLogger("warn");
const logError = logger.error;
logger.error = (msg, options) => {
  errors.push(String(msg));
  logError(msg, options);
};

const server = await createServer({
  root,
  customLogger: logger,
  clearScreen: false,
  // IPv4 only: with "localhost", a second dev server can hold the same port on
  // the other address family, and a request can reach that server instead.
  server: { host: "127.0.0.1" },
});
await server.listen();
const base = server.resolvedUrls?.local?.[0];
if (!base) fail("the dev server reported no local URL");

async function get(path) {
  const res = await fetch(new URL(path, base));
  return { status: res.status, body: await res.text() };
}

// Pre-bundled dependencies are served from /node_modules/.vite/deps/. The
// optimizer may re-run while the first requests are in flight, which answers
// stale URLs with 504, so collect the URLs again after the server goes idle.
async function depUrls() {
  const entry = await get("/src/main.tsx");
  if (entry.status !== 200) fail(`GET /src/main.tsx returned ${entry.status}`);
  return [...new Set(entry.body.match(/\/node_modules\/\.vite\/deps\/[^"'\s]+/g) ?? [])];
}

const index = await get("/");
if (index.status !== 200) fail(`GET / returned ${index.status}`);

await depUrls();
await server.waitForRequestsIdle?.();
const deps = await depUrls();
if (deps.length === 0) fail("/src/main.tsx imports no pre-bundled dependency");

for (const dep of deps) {
  const res = await get(dep);
  if (res.status !== 200) fail(`GET ${dep} returned ${res.status}`);
}

await server.close();
if (errors.length > 0) fail(`Vite logged ${errors.length} error(s); first: ${errors[0]}`);
console.log(`smoke:dev OK: ${base} served /, /src/main.tsx and ${deps.length} pre-bundled dependencies`);
process.exit(0);
