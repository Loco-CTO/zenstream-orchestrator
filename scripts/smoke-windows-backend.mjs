import { spawn } from "node:child_process";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const backend = path.join(root, "dist", "backend");
const source = path.join(backend, "source");
const pythonBase = path.join(backend, "python-base");
const metadata = await mkdtemp(path.join(tmpdir(), "zenstream-launcher-smoke-"));
const runtime = await mkdtemp(path.join(tmpdir(), "zenstream-launcher-venv-"));
await cp(path.join(backend, "venv-template"), runtime, { recursive: true });
const version = (await readFile(path.join(backend, "runtime-version.txt"), "utf8")).trim();
const baseExecutable = path.join(pythonBase, "python.exe");
await writeFile(
  path.join(runtime, "pyvenv.cfg"),
  `home = ${pythonBase}\ninclude-system-site-packages = false\nversion = ${version}\nexecutable = ${baseExecutable}\n`,
);

const executable = path.join(runtime, "Scripts", "python.exe");
const output = [];
const child = spawn(
  executable,
  [path.join(source, "orchestrator", "launcher_entry.py")],
  {
    cwd: source,
    env: {
      ...process.env,
      SECRET_KEY: "launcher-smoke-secret-key-with-more-than-32-characters",
      METADATA_PATH: metadata,
      ORCHESTRATOR_HOST: "127.0.0.1",
      ORCHESTRATOR_PORT: "19088",
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  },
);
child.stdout.on("data", (chunk) => output.push(chunk.toString()));
child.stderr.on("data", (chunk) => output.push(chunk.toString()));

try {
  const deadline = Date.now() + 120_000;
  let ready = false;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) break;
    try {
      const [rootResponse, versionResponse, dashboardResponse] = await Promise.all([
        fetch("http://127.0.0.1:19088/"),
        fetch("http://127.0.0.1:19088/api/version"),
        fetch("http://127.0.0.1:19088/web/login/"),
      ]);
      ready = rootResponse.ok && versionResponse.ok && dashboardResponse.ok;
      if (ready) break;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  if (!ready) throw new Error(`Backend did not become ready.\n${output.join("")}`);
  child.stdin.end("shutdown\n");
  const exitCode = await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error("Graceful shutdown timed out.")), 30_000),
    ),
  ]);
  if (exitCode !== 0) throw new Error(`Backend exited with ${exitCode}.\n${output.join("")}`);
  console.log("Windows source/venv backend smoke test passed.");
} finally {
  if (child.exitCode === null) child.kill();
  await rm(metadata, { recursive: true, force: true });
  await rm(runtime, { recursive: true, force: true });
}
