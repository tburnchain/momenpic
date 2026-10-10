// Runs a Next.js CLI command with NEXT_PUBLIC_ACVM_MOCK=1 (cross-platform; no cross-env needed).
//   node scripts/with-mock.mjs dev    -> mock-mode dev server
//   node scripts/with-mock.mjs build  -> mock-mode production build
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const nextBin = require.resolve("next/dist/bin/next");
const [command = "dev", ...rest] = process.argv.slice(2);

const child = spawn(process.execPath, [nextBin, command, ...rest], {
  stdio: "inherit",
  env: { ...process.env, NEXT_PUBLIC_ACVM_MOCK: "1" },
});
child.on("exit", (code) => process.exit(code ?? 0));
