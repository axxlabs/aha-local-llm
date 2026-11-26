// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Axxlabs Limited
// One browser per person, each with a real profile of its own (SPEC §3.4 "Browser profiles").
//
// A browser keeps everything a site stores (cookies, IndexedDB with its WebCrypto keys, service workers, caches)
// only in its profile folder: an exported copy loses what can't be exported (seen: WhatsApp Web unlinked after
// every restart, its keys gone). So each person gets a Playwright MCP of their own with `--user-data-dir` under
// /profiles, the person's folder inside the encrypted mount (gocryptfs, unlocked by aha-profiles.service). The
// agent asks this supervisor, on the browser's own network, which port a person's browser listens on.
//
//   POST /start  {"id": "<account id>"}      → {"port": N}   starts it if needed (id "probe": no profile, for
//                                                             reading the tool definitions)
//   POST /stop   {"id": ...}                 → {}            stops it; the profile stays
//   POST /forget {"id": ...}                 → {}            stops it and deletes the whole profile (Clear browser)
import { spawn } from "node:child_process";
import { rm } from "node:fs/promises";
import { connect } from "node:net";
import http from "node:http";

const PROFILES = "/profiles";
const FIRST_PORT = 8931;
const ID = /^(?:[a-f0-9]{8,64}|probe)$/;
const browsers = new Map(); // id → { port, child, ready }
const freePorts = [];
let nextPort = FIRST_PORT;

// The browsers never open aha's own services: the search, the model, this supervisor, each other.
const BLOCKED = ["http://aha-llm:8081", "http://aha-browser:8930"]
  .concat(Array.from({ length: 32 }, (_, i) => `http://aha-browser:${FIRST_PORT + i}`))
  .join(";");

function args(id, port) {
  const own = id === "probe" ? ["--isolated"] : ["--user-data-dir", `${PROFILES}/${id}`];
  return [
    "/app/cli.js",
    "--browser", "chromium", "--no-sandbox",
    "--config", "/etc/aha-browser/config.json",
    "--host", "0.0.0.0", "--port", String(port), "--allowed-hosts", `aha-browser:${port}`,
    "--caps", "vision", "--image-responses", "allow",
    "--blocked-origins", BLOCKED,
    ...own,
  ];
}

function listening(port) {
  return new Promise((resolve) => {
    const s = connect(port, "127.0.0.1", () => {
      s.end();
      resolve(true);
    });
    s.on("error", () => resolve(false));
  });
}

async function start(id) {
  const known = browsers.get(id);
  if (known && known.child.exitCode === null) return known.ready;
  const port = freePorts.pop() ?? nextPort++;
  // A profile left locked by a browser that didn't exit cleanly (the container stopped): Chromium refuses it.
  if (id !== "probe") {
    for (const f of ["SingletonLock", "SingletonSocket", "SingletonCookie"]) await rm(`${PROFILES}/${id}/${f}`, { force: true });
  }
  const child = spawn(process.execPath, args(id, port), { stdio: ["ignore", "inherit", "inherit"] });
  const entry = { port, child, ready: null };
  entry.ready = (async () => {
    for (let i = 0; i < 300; i++) {
      if (child.exitCode !== null) throw new Error(`the browser for ${id} exited (${child.exitCode})`);
      if (await listening(port)) return port;
      await new Promise((r) => setTimeout(r, 100));
    }
    throw new Error("the browser didn't start in 30 s");
  })();
  child.on("exit", () => {
    if (browsers.get(id) === entry) browsers.delete(id);
    freePorts.push(port);
  });
  browsers.set(id, entry);
  return entry.ready;
}

async function stop(id) {
  const known = browsers.get(id);
  if (!known) return;
  browsers.delete(id);
  if (known.child.exitCode !== null) return;
  const exited = new Promise((r) => known.child.once("exit", r));
  known.child.kill("SIGINT"); // Playwright MCP closes the browser cleanly: its profile written out (cookies too)
  const late = setTimeout(() => known.child.kill("SIGKILL"), 10_000);
  await exited;
  clearTimeout(late);
}

async function forget(id) {
  if (id === "probe") return;
  await stop(id);
  await rm(`${PROFILES}/${id}`, { recursive: true, force: true });
}

const server = http.createServer(async (req, res) => {
  const reply = (code, body) => {
    res.writeHead(code, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (req.method !== "POST") return reply(405, { error: "POST only" });
  let raw = "";
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 1000) return reply(413, { error: "too large" });
  }
  let id;
  try {
    id = JSON.parse(raw).id;
  } catch {
    return reply(400, { error: "bad JSON" });
  }
  if (typeof id !== "string" || !ID.test(id)) return reply(400, { error: "bad id" });
  try {
    if (req.url === "/start") return reply(200, { port: await start(id) });
    if (req.url === "/stop") return reply(200, (await stop(id), {}));
    if (req.url === "/forget") return reply(200, (await forget(id), {}));
    return reply(404, { error: "unknown" });
  } catch (e) {
    return reply(500, { error: String(e?.message ?? e) });
  }
});

for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, async () => {
    await Promise.all([...browsers.keys()].map(stop)); // every profile closed cleanly
    process.exit(0);
  });
}

server.listen(8930, "0.0.0.0", () => console.log("aha browser supervisor on :8930"));
