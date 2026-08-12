import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const launcherRoot = path.join(projectRoot, "launcher");
const requireFromLauncher = createRequire(path.join(launcherRoot, "package.json"));
const asar = requireFromLauncher("@electron/asar");
const archive = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(
      launcherRoot,
      "release",
      "win-unpacked",
      "resources",
      "app.asar",
    );

const packageDocument = JSON.parse(
  asar.extractFile(archive, "package.json").toString("utf8"),
);
if (packageDocument.main !== "dist-electron/main/main.js") {
  throw new Error(`Unexpected packaged main entry: ${packageDocument.main}`);
}

const mainPath = packageDocument.main.replaceAll("/", path.sep);
const mainSource = asar.extractFile(archive, mainPath).toString("utf8");
if (!mainSource.includes(".venv-preparing") || !mainSource.includes("pathToFileURL")) {
  throw new Error("Packaged main process is missing launcher startup safeguards.");
}

const renderer = asar
  .extractFile(archive, path.join("dist", "index.html"))
  .toString("utf8");
if (!renderer.includes("ZenStream Orchestrator")) {
  throw new Error("Packaged renderer entry is invalid.");
}

console.log(
  `Validated app.asar: ${asar.listPackage(archive).length} entries, main and renderer readable.`,
);
