import { appendFile, mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { LauncherLogStore } from "./log-store";
import type { LogSource } from "../shared";

const stores: LauncherLogStore[] = [];
const directories: string[] = [];

afterEach(async () => {
  await Promise.all(stores.splice(0).map((store) => store.close()));
  await Promise.all(
    directories.splice(0).map(async (directory) => {
      const { rm } = await import("node:fs/promises");
      await rm(directory, { recursive: true, force: true });
    }),
  );
});

async function createStore(options?: {
  segmentBytes?: number;
  maxSegments?: number;
}) {
  const directory = await mkdtemp(
    path.join(os.tmpdir(), "zenstream-launcher-"),
  );
  directories.push(directory);
  const store = new LauncherLogStore(directory, options);
  stores.push(store);
  return { store, directory };
}

async function append(
  store: LauncherLogStore,
  message: string,
  source: LogSource = "stdout",
) {
  return store.append({
    timestamp: "2026-01-01T00:00:00.000Z",
    source,
    message,
  });
}

describe("LauncherLogStore", () => {
  it("pages older and newer records without duplicates", async () => {
    const { store } = await createStore();
    const first = await append(store, "first");
    await append(store, "second");
    await append(store, "third");

    const tail = await store.read({ direction: "older", limit: 2 });
    expect(tail.entries.map((entry) => entry.message)).toEqual([
      "second",
      "third",
    ]);
    expect(tail.hasOlder).toBe(true);

    const older = await store.read({
      direction: "older",
      cursor: tail.olderCursor ?? undefined,
      limit: 2,
    });
    expect(older.entries.map((entry) => entry.message)).toEqual(["first"]);

    const newer = await store.read({
      direction: "newer",
      cursor: first.afterCursor,
      limit: 2,
    });
    expect(newer.entries.map((entry) => entry.message)).toEqual([
      "second",
      "third",
    ]);
  });

  it("filters across retained segments and reports expired cursors", async () => {
    const { store } = await createStore({ segmentBytes: 180, maxSegments: 2 });
    await append(store, "keep-one", "stderr");
    await append(store, "drop-one", "stdout");
    await append(store, "keep-two", "stderr");
    const page = await store.read({
      direction: "older",
      source: "stderr",
      query: "keep",
    });
    expect(page.entries.map((entry) => entry.message)).toEqual([
      "keep-one",
      "keep-two",
    ]);

    const expired = await store.read({
      direction: "older",
      cursor: "eyJzZWdtZW50SWQiOiJtaXNzaW5nIiwib2Zmc2V0IjowfQ",
    });
    expect(expired.cursorExpired).toBe(true);
  });

  it("ignores malformed lines and exports without credential material", async () => {
    const { store, directory } = await createStore();
    await append(store, "Password: <redacted>", "launcher");
    await store.close();
    const segment = (await import("node:fs/promises"))
      .readdir(directory)
      .then((names) => names.find((name) => name.endsWith(".ndjson"))!);
    await appendFile(path.join(directory, await segment), "not-json\n", "utf8");
    const reopened = new LauncherLogStore(directory);
    stores.push(reopened);
    const page = await reopened.read({ direction: "older", limit: 10 });
    expect(page.entries).toHaveLength(1);

    const destination = path.join(directory, "export.log");
    await reopened.exportTo(destination);
    expect(await readFile(destination, "utf8")).toContain(
      "Password: <redacted>",
    );
  });

  it("clears only launcher segments", async () => {
    const { store, directory } = await createStore();
    await append(store, "line");
    const backendLog = path.join(directory, "orchestrator.log");
    await writeFile(backendLog, "backend", "utf8");
    await store.clear();
    expect(await readFile(backendLog, "utf8")).toBe("backend");
    expect((await store.read({ direction: "older" })).entries).toEqual([]);
  });
});
