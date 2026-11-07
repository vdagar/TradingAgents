// The Python side, set up on first run: a pinned uv, a private Python 3.12
// environment, and the tradingagents wheel shipped inside this extension.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

const UV_VERSION = "0.13.7";
// From the uv release's own .sha256 files.
const UV_ASSETS: Record<string, [string, string]> = {
  "darwin-arm64": ["uv-aarch64-apple-darwin.tar.gz", "81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731"],
  "darwin-x64": ["uv-x86_64-apple-darwin.tar.gz", "20c47e4c1f630b8abaff8c291fa9b9aa300fef92c4fbff95adf198b55287cda7"],
  "linux-x64": ["uv-x86_64-unknown-linux-gnu.tar.gz", "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83"],
  "linux-arm64": ["uv-aarch64-unknown-linux-gnu.tar.gz", "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc"],
  "win32-x64": ["uv-x86_64-pc-windows-msvc.zip", "0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021"],
  "win32-arm64": ["uv-aarch64-pc-windows-msvc.zip", "505d9e65e7692bac3ddf98c66754aa26c5050c409c184af30a881a0a72cc85e3"],
};

const exe = (name: string) => (process.platform === "win32" ? `${name}.exe` : name);

function run(cmd: string, args: string[], env: NodeJS.ProcessEnv): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { env, stdio: ["ignore", "ignore", "pipe"] });
    let err = "";
    child.stderr.on("data", (d) => (err = (err + d).slice(-2000)));
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`${path.basename(cmd)} ${args[0]} failed: ${err}`))));
  });
}

function findFile(dir: string, name: string): string | undefined {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isFile() && entry.name === name) return full;
    if (entry.isDirectory()) {
      const found = findFile(full, name);
      if (found) return found;
    }
  }
  return undefined;
}

async function ensureUv(storage: string): Promise<string> {
  const dir = path.join(storage, `uv-${UV_VERSION}`);
  const existing = fs.existsSync(dir) ? findFile(dir, exe("uv")) : undefined;
  if (existing) return existing;
  const asset = UV_ASSETS[`${process.platform}-${process.arch}`];
  if (!asset) throw new Error(`No uv build for ${process.platform}-${process.arch}`);
  const [name, sha256] = asset;
  const response = await fetch(`https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${name}`);
  if (!response.ok) throw new Error(`uv download failed: HTTP ${response.status}`);
  const body = Buffer.from(await response.arrayBuffer());
  if (createHash("sha256").update(body).digest("hex") !== sha256) {
    throw new Error("uv download does not match its pinned SHA-256; not running it");
  }
  const tmp = `${dir}.tmp`;
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });
  const archive = path.join(tmp, name);
  fs.writeFileSync(archive, body);
  await run("tar", ["-xf", archive, "-C", tmp], process.env); // bsdtar on Windows reads .zip too
  fs.rmSync(archive);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.renameSync(tmp, dir);
  const uv = findFile(dir, exe("uv"));
  if (!uv) throw new Error("uv archive had no uv binary");
  return uv;
}

export interface Runtime {
  python: string;
  uvDir: string;
  env: NodeJS.ProcessEnv;
}

/** uv, Python and the wheel, installed once per extension version under `storage`. */
export async function ensureRuntime(storage: string, extensionDir: string, version: string,
  progress: (message: string) => void): Promise<Runtime> {
  const uv = await ensureUv(storage);
  const env = {
    ...process.env,
    UV_PYTHON_INSTALL_DIR: path.join(storage, "python"),
    UV_CACHE_DIR: path.join(storage, "uv-cache"),
  };
  const venv = path.join(storage, `env-${version}`);
  const python = process.platform === "win32" ? path.join(venv, "Scripts", "python.exe") : path.join(venv, "bin", "python");
  const ready = path.join(venv, ".ready");
  if (!fs.existsSync(ready)) {
    progress("First run: installing Python 3.12 and TradingAgents (about a minute)…");
    const wheels = path.join(extensionDir, "python");
    const wheel = fs.readdirSync(wheels).find((f) => f.endsWith(".whl"));
    if (!wheel) throw new Error("the extension package has no tradingagents wheel");
    fs.rmSync(venv, { recursive: true, force: true });
    await run(uv, ["venv", "--python", "3.12", venv], env);
    await run(uv, ["pip", "install", "--python", python, path.join(wheels, wheel)], env);
    fs.writeFileSync(ready, "");
  }
  return { python, uvDir: path.dirname(uv), env };
}
