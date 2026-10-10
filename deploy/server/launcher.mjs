// ACVM (momenpic) entry point for the single-host appctl/Caddy server — `ENTRY` of systemd `app@momenpic`.
//
// appctl gives every project one port ($PORT on $HOST) and runs `node $ENTRY`. ACVM is three processes, so this
// launcher supervises them and puts one same-origin front on $PORT:
//
//   /api/* /hub/* /r/* /media/* /sandbox/* /healthz /readyz   → FastAPI (uvicorn)      127.0.0.1:$ACVM_API_PORT
//   everything else                                            → Next.js console       127.0.0.1:$ACVM_WEB_PORT
//   /metrics                                                   → 404 (internal only)
//   background loops (publish scheduler, outbox relay, link health, reports)  → `python -m acvm.orchestration.loops`
//
// If any child dies the launcher exits non-zero and systemd (Restart=always) restarts the whole set.
// Zero dependencies: node:http streaming proxy (uploads/videos are piped, never buffered).

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PORT = Number(process.env.PORT || 4046);
const HOST = process.env.HOST || "127.0.0.1";
const API_PORT = Number(process.env.ACVM_API_PORT || PORT + 1100);
const WEB_PORT = Number(process.env.ACVM_WEB_PORT || PORT + 1200);
const LOOPS_METRICS_PORT = Number(process.env.ACVM_LOOPS_METRICS_PORT || PORT + 1300);
const RUN_LOOPS = !/^(0|false|no)$/i.test(process.env.ACVM_RUN_LOOPS || "1");
const PY = join(ROOT, ".venv", "bin", "python");
const WEB = join(ROOT, "console", ".next", "standalone", "server.js");

const API_PREFIXES = ["/api/", "/hub/", "/r/", "/media/", "/sandbox/"];
const API_EXACT = new Set(["/api", "/healthz", "/readyz"]);
const BLOCKED = new Set(["/metrics"]);

const log = (msg, extra = {}) =>
  console.log(JSON.stringify({ ts: new Date().toISOString(), src: "launcher", msg, ...extra }));

for (const [what, path] of [["python venv", PY], ["console build", WEB]]) {
  if (!existsSync(path)) {
    log("missing", { what, path, fix: "run deploy/server/install.sh" });
    process.exit(1);
  }
}

// ── children ─────────────────────────────────────────────────────────────
const children = new Map();
let stopping = false;

function start(name, cmd, args, env) {
  const child = spawn(cmd, args, { cwd: ROOT, env: { ...process.env, ...env }, stdio: ["ignore", "inherit", "inherit"] });
  children.set(name, child);
  log("started", { child: name, pid: child.pid });
  child.on("exit", (code, signal) => {
    children.delete(name);
    if (stopping) return;
    log("child exited — restarting the whole unit", { child: name, code, signal });
    shutdown(1);
  });
  return child;
}

start("api", PY, ["-m", "uvicorn", "acvm.api.app:app", "--host", "127.0.0.1", "--port", String(API_PORT),
  "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--no-server-header", "--timeout-keep-alive", "65"]);
start("web", process.execPath, [WEB], { PORT: String(WEB_PORT), HOSTNAME: "127.0.0.1", NODE_ENV: "production" });
if (RUN_LOOPS) {
  start("loops", PY, ["-m", "acvm.orchestration.loops"],
    { ACVM_LOOPS_METRICS_PORT: String(LOOPS_METRICS_PORT), ACVM_LOOPS_METRICS_ADDR: "127.0.0.1" });
}

function shutdown(code) {
  if (stopping) return;
  stopping = true;
  for (const c of children.values()) c.kill("SIGTERM");
  setTimeout(() => {
    for (const c of children.values()) c.kill("SIGKILL");
    process.exit(code);
  }, 8000).unref();
  if (children.size === 0) process.exit(code);
  const timer = setInterval(() => {
    if (children.size === 0) {
      clearInterval(timer);
      process.exit(code);
    }
  }, 100);
}
process.on("SIGTERM", () => shutdown(0));
process.on("SIGINT", () => shutdown(0));

// ── readiness ────────────────────────────────────────────────────────────
const ready = { api: false, web: false };

function probe(port) {
  return new Promise((ok) => {
    const s = net.connect({ port, host: "127.0.0.1" }, () => {
      s.destroy();
      ok(true);
    });
    s.on("error", () => ok(false));
    s.setTimeout(1000, () => {
      s.destroy();
      ok(false);
    });
  });
}

const readyTimer = setInterval(async () => {
  ready.api = ready.api || (await probe(API_PORT));
  ready.web = ready.web || (await probe(WEB_PORT));
  if (ready.api && ready.web) {
    clearInterval(readyTimer);
    log("ready", { port: PORT, api: API_PORT, web: WEB_PORT });
  }
}, 500);

// ── proxy ────────────────────────────────────────────────────────────────
function target(pathname) {
  if (API_EXACT.has(pathname) || API_PREFIXES.some((p) => pathname.startsWith(p))) return ["api", API_PORT];
  return ["web", WEB_PORT];
}

const HOP = new Set(["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer"]);

function forward(req, res, attempt = 0) {
  const pathname = (req.url || "/").split("?")[0];
  if (BLOCKED.has(pathname)) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
    return;
  }
  const [name, port] = target(pathname);
  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) if (!HOP.has(k.toLowerCase())) headers[k] = v;
  const fwdFor = req.headers["x-forwarded-for"];
  headers["x-forwarded-for"] = fwdFor ? `${fwdFor}, ${req.socket.remoteAddress}` : req.socket.remoteAddress;
  headers["x-forwarded-proto"] = req.headers["x-forwarded-proto"] || "https";
  headers["x-forwarded-host"] = req.headers["x-forwarded-host"] || req.headers.host || "";

  const up = http.request({ host: "127.0.0.1", port, method: req.method, path: req.url, headers }, (upRes) => {
    const out = {};
    for (const [k, v] of Object.entries(upRes.headers)) if (!HOP.has(k.toLowerCase())) out[k] = v;
    res.writeHead(upRes.statusCode || 502, out);
    upRes.pipe(res);
  });
  up.on("error", (err) => {
    // During (re)start a child may not listen yet: hold idempotent requests for up to ~20 s instead of failing.
    const idempotent = req.method === "GET" || req.method === "HEAD";
    if (err.code === "ECONNREFUSED" && idempotent && attempt < 40 && !res.headersSent) {
      setTimeout(() => forward(req, res, attempt + 1), 500);
      return;
    }
    if (!res.headersSent) {
      res.writeHead(name === "api" ? 503 : 502, { "content-type": "text/plain; charset=utf-8", "retry-after": "5" });
      res.end(ready[name] ? "upstream error" : "starting — try again in a few seconds");
    } else {
      res.destroy(err);
    }
  });
  req.on("aborted", () => up.destroy());
  if (attempt === 0) req.pipe(up);
  else up.end();
}

const server = http.createServer((req, res) => forward(req, res));
server.requestTimeout = 0; // AI/media requests may run for minutes; Caddy/Cloudflare enforce the outer limits
server.headersTimeout = 65_000;
server.keepAliveTimeout = 65_000;
server.listen(PORT, HOST, () => log("listening", { host: HOST, port: PORT, api: API_PORT, web: WEB_PORT, loops: RUN_LOOPS }));
