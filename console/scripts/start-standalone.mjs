// Runs the production build the same way the Docker image does (output: "standalone"):
// copies .next/static and public next to the standalone server, then starts it.
//   npm run build && npm start            (PORT=3000 by default)
import { spawn } from "node:child_process";
import { cpSync, existsSync, rmSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const standalone = join(root, ".next", "standalone");
if (!existsSync(join(standalone, "server.js"))) {
  console.error("No standalone build found — run `npm run build` first.");
  process.exit(1);
}
for (const [from, to] of [
  [join(root, ".next", "static"), join(standalone, ".next", "static")],
  [join(root, "public"), join(standalone, "public")],
]) {
  rmSync(to, { recursive: true, force: true });
  cpSync(from, to, { recursive: true });
}
const child = spawn(process.execPath, [join(standalone, "server.js")], {
  stdio: "inherit",
  env: { PORT: "3000", HOSTNAME: "0.0.0.0", ...process.env },
});
child.on("exit", (code) => process.exit(code ?? 0));
