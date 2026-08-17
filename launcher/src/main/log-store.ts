import { createReadStream, createWriteStream, type WriteStream } from "node:fs";
import { once } from "node:events";
import { mkdir, readdir, stat, unlink } from "node:fs/promises";
import readline from "node:readline";
import path from "node:path";
import { randomUUID } from "node:crypto";
import type {
  LogCursor,
  LogEntry,
  LogPage,
  LogSource,
  PagedLogEntry,
  ReadLogsRequest,
} from "../shared";

export const LOG_SEGMENT_BYTES = 10 * 1024 * 1024;
export const MAX_LOG_SEGMENTS = 5;
const LOG_FILE_PREFIX = "zenstream-launcher-";
const LOG_FILE_SUFFIX = ".ndjson";
const MAX_PAGE_SIZE = 500;

interface Segment {
  id: string;
  filePath: string;
  size: number;
}

interface CursorPosition {
  segmentId: string;
  offset: number;
}

interface ScannedRecord {
  segment: Segment;
  start: number;
  bytes: number;
  timestamp: string;
  source: LogSource;
  message: string;
}

export interface LogAppendInput {
  timestamp: string;
  source: LogSource;
  message: string;
}

export class LauncherLogStore {
  private segments: Segment[] = [];
  private active: Segment | null = null;
  private stream: WriteStream | null = null;
  private ready: Promise<void>;
  private operationTail: Promise<void> = Promise.resolve();

  private readonly segmentBytes: number;
  private readonly maxSegments: number;

  constructor(
    private directory: string,
    options: { segmentBytes?: number; maxSegments?: number } = {},
  ) {
    this.segmentBytes = options.segmentBytes ?? LOG_SEGMENT_BYTES;
    this.maxSegments = options.maxSegments ?? MAX_LOG_SEGMENTS;
    this.ready = this.load();
  }

  async append(input: LogAppendInput): Promise<PagedLogEntry> {
    let result!: PagedLogEntry;
    const operation = this.enqueue(async () => {
      await this.ensureReady();
      const line = `${JSON.stringify({
        v: 1,
        timestamp: input.timestamp,
        source: input.source,
        message: input.message,
      })}\n`;
      const data = Buffer.from(line, "utf8");
      await this.ensureWritable(data.byteLength);
      const segment = this.active!;
      const start = segment.size;
      await writeWithBackpressure(this.stream!, data);
      segment.size += data.byteLength;
      result = {
        id: `${segment.id}:${start}`,
        timestamp: input.timestamp,
        source: input.source,
        message: input.message,
        beforeCursor: encodeCursor({ segmentId: segment.id, offset: start }),
        afterCursor: encodeCursor({
          segmentId: segment.id,
          offset: start + data.byteLength,
        }),
      };
    });
    await operation;
    return result;
  }

  async read(request: ReadLogsRequest): Promise<LogPage> {
    await this.ensureReady();
    await this.operationTail;
    if (!request || !["older", "newer"].includes(request.direction))
      throw new Error("Invalid launcher log page direction.");
    const limitValue = typeof request.limit === "number" ? request.limit : 250;
    const limit = Math.max(
      1,
      Math.min(
        MAX_PAGE_SIZE,
        Number.isFinite(limitValue) ? Math.floor(limitValue) : 250,
      ),
    );
    const query =
      typeof request.query === "string"
        ? request.query.slice(0, 512).trim().toLowerCase()
        : "";
    const source = ["all", "stdout", "stderr", "launcher"].includes(
      String(request.source),
    )
      ? (request.source ?? "all")
      : "all";
    const cursor =
      typeof request.cursor === "string"
        ? request.cursor.slice(0, 512)
        : undefined;
    const boundary = cursor ? decodeCursor(cursor) : null;
    if (cursor && !this.isValidCursor(boundary)) {
      return {
        entries: [],
        olderCursor: null,
        newerCursor: null,
        hasOlder: false,
        hasNewer: false,
        cursorExpired: true,
      };
    }

    const entries: PagedLogEntry[] = [];
    let overflow = false;
    for await (const record of this.records()) {
      if (!this.matchesDirection(record, request.direction, boundary)) continue;
      if (source !== "all" && record.source !== source) continue;
      if (query && !record.message.toLowerCase().includes(query)) continue;

      const entry = this.toEntry(record);
      if (request.direction === "older") {
        entries.push(entry);
        if (entries.length > limit) {
          entries.shift();
          overflow = true;
        }
      } else if (entries.length < limit) {
        entries.push(entry);
      } else {
        overflow = true;
      }
    }

    const olderCursor = entries[0]?.beforeCursor ?? null;
    const newerCursor = entries.at(-1)?.afterCursor ?? null;
    return {
      entries,
      olderCursor,
      newerCursor,
      hasOlder: request.direction === "older" ? overflow : Boolean(boundary),
      hasNewer: request.direction === "newer" ? overflow : Boolean(boundary),
      cursorExpired: false,
    };
  }

  async setDirectory(directory: string): Promise<void> {
    await this.ensureReady();
    await this.enqueue(async () => {
      await this.closeStream();
      this.directory = directory;
      this.segments = [];
      this.active = null;
      this.ready = this.load();
      await this.ready;
    });
  }

  async clear(): Promise<void> {
    await this.ensureReady();
    await this.enqueue(async () => {
      await this.closeStream();
      for (const segment of this.segments)
        await removeIfPresent(segment.filePath);
      this.segments = [];
      this.active = null;
      await this.createSegment();
    });
  }

  async exportTo(destination: string): Promise<void> {
    await this.ensureReady();
    await this.operationTail;
    const output = createWriteStream(destination, { encoding: "utf8" });
    try {
      for await (const record of this.records()) {
        const message = redactCredentialLine(record.message);
        await writeWithBackpressure(
          output,
          Buffer.from(
            `${record.timestamp} [${record.source}] ${message}\n`,
            "utf8",
          ),
        );
      }
    } finally {
      await endStream(output);
    }
  }

  async close(): Promise<void> {
    await this.ensureReady();
    await this.operationTail;
    await this.closeStream();
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.operationTail.then(operation, operation);
    this.operationTail = next.catch(() => undefined);
    return next;
  }

  private async ensureReady(): Promise<void> {
    await this.ready;
  }

  private async load(): Promise<void> {
    await mkdir(this.directory, { recursive: true });
    const names = await readdir(this.directory);
    const loaded: Segment[] = [];
    for (const name of names) {
      if (!name.startsWith(LOG_FILE_PREFIX) || !name.endsWith(LOG_FILE_SUFFIX))
        continue;
      const id = name.slice(LOG_FILE_PREFIX.length, -LOG_FILE_SUFFIX.length);
      if (!id || !/^[a-z0-9-]+$/i.test(id)) continue;
      const filePath = path.join(this.directory, name);
      const details = await stat(filePath).catch(() => null);
      if (details?.isFile()) loaded.push({ id, filePath, size: details.size });
    }
    loaded.sort((left, right) => left.id.localeCompare(right.id));
    this.segments = loaded.slice(-this.maxSegments);
    for (const segment of loaded.slice(0, -this.maxSegments))
      await removeIfPresent(segment.filePath);
    await this.createSegment();
  }

  private async createSegment(): Promise<void> {
    const id = `${Date.now().toString(36).padStart(12, "0")}-${randomUUID().replaceAll("-", "")}`;
    const segment: Segment = {
      id,
      filePath: path.join(
        this.directory,
        `${LOG_FILE_PREFIX}${id}${LOG_FILE_SUFFIX}`,
      ),
      size: 0,
    };
    this.segments.push(segment);
    this.active = segment;
    this.stream = createWriteStream(segment.filePath, { flags: "a" });
    this.stream.on("error", () => undefined);
    while (this.segments.length > this.maxSegments) {
      const retired = this.segments.shift()!;
      if (retired !== segment) await removeIfPresent(retired.filePath);
    }
  }

  private async ensureWritable(bytes: number): Promise<void> {
    if (!this.active || !this.stream) await this.createSegment();
    if (
      this.active!.size > 0 &&
      this.active!.size + bytes > this.segmentBytes
    ) {
      await this.closeStream();
      await this.createSegment();
    }
  }

  private async closeStream(): Promise<void> {
    const stream = this.stream;
    this.stream = null;
    this.active = null;
    if (stream) await endStream(stream);
  }

  private async *records(): AsyncGenerator<ScannedRecord> {
    for (const segment of this.segments) {
      const input = createReadStream(segment.filePath, { encoding: "utf8" });
      const reader = readline.createInterface({ input, crlfDelay: Infinity });
      let offset = 0;
      try {
        for await (const line of reader) {
          const bytes = Buffer.byteLength(line, "utf8") + 1;
          const parsed = parseRecord(line);
          if (parsed) yield { segment, start: offset, bytes, ...parsed };
          offset += bytes;
        }
      } finally {
        reader.close();
        input.destroy();
      }
    }
  }

  private matchesDirection(
    record: ScannedRecord,
    direction: ReadLogsRequest["direction"],
    boundary: CursorPosition | null,
  ): boolean {
    if (!boundary) return direction === "older";
    const recordIndex = this.segments.findIndex(
      (segment) => segment.id === record.segment.id,
    );
    const boundaryIndex = this.segments.findIndex(
      (segment) => segment.id === boundary.segmentId,
    );
    const comparison =
      recordIndex === boundaryIndex
        ? record.start - boundary.offset
        : recordIndex - boundaryIndex;
    return direction === "older" ? comparison < 0 : comparison >= 0;
  }

  private isValidCursor(
    cursor: CursorPosition | null,
  ): cursor is CursorPosition {
    if (!cursor || !Number.isSafeInteger(cursor.offset) || cursor.offset < 0)
      return false;
    const segment = this.segments.find(
      (candidate) => candidate.id === cursor.segmentId,
    );
    return Boolean(segment && cursor.offset <= segment.size);
  }

  private toEntry(record: ScannedRecord): PagedLogEntry {
    return {
      id: `${record.segment.id}:${record.start}`,
      timestamp: record.timestamp,
      source: record.source,
      message: record.message,
      beforeCursor: encodeCursor({
        segmentId: record.segment.id,
        offset: record.start,
      }),
      afterCursor: encodeCursor({
        segmentId: record.segment.id,
        offset: record.start + record.bytes,
      }),
    };
  }
}

function parseRecord(
  line: string,
): Omit<ScannedRecord, "segment" | "start" | "bytes"> | null {
  if (!line.trim()) return null;
  try {
    const value = JSON.parse(line) as Record<string, unknown>;
    if (
      value.v !== 1 ||
      typeof value.timestamp !== "string" ||
      !["stdout", "stderr", "launcher"].includes(String(value.source)) ||
      typeof value.message !== "string"
    )
      return null;
    return {
      timestamp: value.timestamp,
      source: value.source as LogSource,
      message: value.message,
    };
  } catch {
    return null;
  }
}

function encodeCursor(position: CursorPosition): LogCursor {
  return Buffer.from(JSON.stringify(position), "utf8").toString("base64url");
}

function decodeCursor(value: LogCursor): CursorPosition | null {
  try {
    const parsed = JSON.parse(
      Buffer.from(value, "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    if (
      typeof parsed.segmentId !== "string" ||
      typeof parsed.offset !== "number"
    )
      return null;
    return { segmentId: parsed.segmentId, offset: parsed.offset };
  } catch {
    return null;
  }
}

function redactCredentialLine(message: string): string {
  return /^(Username|Password):\s*/i.test(message)
    ? `${message.split(":", 1)[0]}: <redacted>`
    : message;
}

async function removeIfPresent(filePath: string): Promise<void> {
  await unlink(filePath).catch(() => undefined);
}

async function writeWithBackpressure(
  stream: WriteStream,
  data: Buffer,
): Promise<void> {
  let accepted = false;
  let drain: Promise<unknown[]> | null = null;
  await new Promise<void>((resolve, reject) => {
    accepted = stream.write(data, (error?: Error | null) => {
      if (error) reject(error);
      else resolve();
    });
    if (!accepted) drain = once(stream, "drain");
  });
  if (!accepted && drain) await drain;
}

async function endStream(stream: WriteStream): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    stream.once("error", reject);
    stream.end(() => resolve());
  }).catch((error) => {
    if ((error as NodeJS.ErrnoException)?.code !== "ERR_STREAM_PREMATURE_CLOSE")
      throw error;
  });
}
