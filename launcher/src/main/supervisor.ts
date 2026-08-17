import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { EventEmitter } from "node:events";
import path from "node:path";
import readline from "node:readline";
import { execFile } from "node:child_process";
import {
  ENVIRONMENT_KEYS,
  type BootstrapCredentials,
  type LauncherState,
  type LogSource,
  type PagedLogEntry,
} from "../shared";

export interface BackendCommand {
  command: string;
  args: string[];
  cwd: string;
}

export interface SupervisorOptions {
  resolveCommand: () => BackendCommand;
  getEnvironment: () => NodeJS.ProcessEnv;
  persistLog?: (entry: {
    timestamp: string;
    source: LogSource;
    message: string;
  }) => Promise<PagedLogEntry>;
}

export function dashboardUrlFor(environment: NodeJS.ProcessEnv): string {
  let host = environment.ORCHESTRATOR_HOST || "127.0.0.1";
  if (["0.0.0.0", "::", "[::]"].includes(host)) host = "127.0.0.1";
  if (host.includes(":") && !host.startsWith("[")) host = `[${host}]`;
  return `http://${host}:${environment.ORCHESTRATOR_PORT || "9088"}/web/`;
}

export function buildBackendEnvironment(
  base: NodeJS.ProcessEnv,
  configured: NodeJS.ProcessEnv,
): NodeJS.ProcessEnv {
  const result = { ...base };
  for (const key of [...ENVIRONMENT_KEYS, "SECRET_KEY", "USE_RELOADER"]) {
    delete result[key];
  }
  for (const [key, value] of Object.entries(configured)) {
    if (value !== undefined && value !== "") result[key] = value;
  }
  result.PYTHONUNBUFFERED = "1";
  result.PYTHONUTF8 = "1";
  return result;
}

export function crashRetryDelay(attempt: number): number | null {
  return [1_000, 3_000, 10_000][attempt] ?? null;
}

export function launcherLogLine(message: string): string {
  const credentialLabel = /^(Username|Password):\s*/i.exec(message)?.[1];
  return credentialLabel ? `${credentialLabel}: <redacted>` : message;
}

export class BackendSupervisor extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private readinessGeneration = 0;
  private retryAttempt = 0;
  private retryTimer: NodeJS.Timeout | null = null;
  private stableTimer: NodeJS.Timeout | null = null;
  private intentionalStop = false;
  private recentOutput: string[] = [];
  private credentials: Partial<BootstrapCredentials> = {};
  private state: LauncherState = {
    status: "stopped",
    pid: null,
    error: null,
    restartRequired: false,
    backendVersion: null,
    dashboardUrl: "http://127.0.0.1:9088/web/",
    startedAt: null,
  };

  constructor(private readonly options: SupervisorOptions) {
    super();
  }

  snapshot(): LauncherState {
    return { ...this.state };
  }

  setRestartRequired(required: boolean): void {
    this.update({ restartRequired: required });
  }

  async start(resetRetries = true): Promise<LauncherState> {
    if (this.child || ["starting", "running"].includes(this.state.status)) {
      return this.snapshot();
    }
    this.clearRetryTimer();
    if (resetRetries) this.retryAttempt = 0;
    this.intentionalStop = false;
    const command = this.options.resolveCommand();
    const configured = this.options.getEnvironment();
    const environment = buildBackendEnvironment(process.env, configured);
    const dashboardUrl = dashboardUrlFor(configured);
    this.credentials = {};
    this.recentOutput = [];
    this.update({
      status: "starting",
      error: null,
      pid: null,
      backendVersion: null,
      dashboardUrl,
      startedAt: null,
    });
    this.log("launcher", `Starting backend from ${command.command}`);

    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(command.command, command.args, {
        cwd: command.cwd,
        env: environment,
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (error) {
      this.update({ status: "error", error: String(error) });
      return this.snapshot();
    }
    this.child = child;
    this.update({ pid: child.pid ?? null });
    this.attachOutput(child, child.stdout, "stdout");
    this.attachOutput(child, child.stderr, "stderr");
    child.once("error", (error) => {
      this.log("launcher", `Backend process error: ${error.message}`);
      if (this.child === child) {
        this.child = null;
        ++this.readinessGeneration;
        this.update({
          status: "error",
          pid: null,
          error: `Could not start the backend: ${error.message}`,
          startedAt: null,
        });
      }
    });
    child.once("exit", (code, signal) => this.handleExit(child, code, signal));
    void this.pollReadiness(child, ++this.readinessGeneration);
    return this.snapshot();
  }

  async stop(): Promise<LauncherState> {
    this.intentionalStop = true;
    this.clearRetryTimer();
    this.clearStableTimer();
    ++this.readinessGeneration;
    const child = this.child;
    if (!child) {
      this.update({
        status: "stopped",
        pid: null,
        error: null,
        startedAt: null,
      });
      return this.snapshot();
    }
    this.update({ status: "stopping", error: null });
    this.log("launcher", "Requesting graceful backend shutdown");
    const exited = new Promise<void>((resolve) =>
      child.once("exit", () => resolve()),
    );
    try {
      child.stdin.end("shutdown\n");
    } catch {
      // A concurrent backend exit will be handled by the exit listener.
    }
    await Promise.race([exited, delay(20_000)]);
    if (this.child === child && child.exitCode === null && child.pid) {
      this.log(
        "launcher",
        "Graceful shutdown timed out; terminating backend process tree",
      );
      await terminateWindowsProcessTree(child.pid);
      await Promise.race([exited, delay(5_000)]);
    }
    return this.snapshot();
  }

  async restart(): Promise<LauncherState> {
    this.update({ status: "restarting", error: null });
    await this.stop();
    this.intentionalStop = false;
    this.update({ restartRequired: false });
    return this.start();
  }

  private attachOutput(
    child: ChildProcessWithoutNullStreams,
    stream: NodeJS.ReadableStream,
    source: LogSource,
  ): void {
    const reader = readline.createInterface({ input: stream });
    void (async () => {
      for await (const message of reader) {
        if (this.child !== child) break;
        this.captureCredentials(message);
        const safeMessage = launcherLogLine(message);
        this.recentOutput.push(safeMessage);
        if (this.recentOutput.length > 80) this.recentOutput.shift();
        await this.log(source, safeMessage);
      }
    })().catch((error) => {
      this.emit("log-error", error);
    });
  }

  private captureCredentials(message: string): void {
    const username = /^Username:\s*(.+)$/i.exec(message)?.[1]?.trim();
    const password = /^Password:\s*(.+)$/i.exec(message)?.[1]?.trim();
    if (username) this.credentials.username = username;
    if (password) this.credentials.password = password;
    if (this.credentials.username && this.credentials.password) {
      this.emit("credentials", { ...this.credentials } as BootstrapCredentials);
      this.credentials = {};
    }
  }

  private async pollReadiness(
    child: ChildProcessWithoutNullStreams,
    generation: number,
  ): Promise<void> {
    const versionUrl = new URL("/api/version", this.state.dashboardUrl);
    while (this.child === child && generation === this.readinessGeneration) {
      try {
        const response = await fetch(versionUrl, {
          signal: AbortSignal.timeout(1_500),
        });
        if (response.ok) {
          const payload = (await response.json()) as {
            version?: unknown;
            main?: unknown;
          };
          const version =
            typeof payload.version === "string" ? payload.version : "unknown";
          const main =
            typeof payload.main === "number" && payload.main > 0
              ? `+main.${payload.main}`
              : "";
          this.update({
            status: "running",
            error: null,
            backendVersion: `${version}${main}`,
            startedAt: new Date().toISOString(),
          });
          this.log("launcher", `Backend ready at ${this.state.dashboardUrl}`);
          this.clearStableTimer();
          this.stableTimer = setTimeout(() => {
            this.retryAttempt = 0;
          }, 60_000);
          return;
        }
      } catch {
        // Startup migrations and socket binding may still be in progress.
      }
      await delay(500);
    }
  }

  private handleExit(
    child: ChildProcessWithoutNullStreams,
    code: number | null,
    signal: NodeJS.Signals | null,
  ): void {
    if (this.child !== child) return;
    const wasRunning = this.state.status === "running";
    this.child = null;
    ++this.readinessGeneration;
    this.clearStableTimer();
    const description = `Backend exited${code === null ? "" : ` with code ${code}`}${signal ? ` (${signal})` : ""}.`;
    this.log("launcher", description);
    if (this.intentionalStop) {
      this.update({
        status: "stopped",
        pid: null,
        error: null,
        startedAt: null,
      });
      return;
    }

    const portConflict = this.recentOutput.some((line) =>
      /address already in use|winerror\s*10048|attempting to bind/i.test(line),
    );
    if (!wasRunning || portConflict) {
      this.update({
        status: "error",
        pid: null,
        startedAt: null,
        error: portConflict
          ? "The configured port is already in use. Choose another port and restart."
          : description,
      });
      return;
    }

    const retryDelay = crashRetryDelay(this.retryAttempt++);
    if (retryDelay === null) {
      this.update({
        status: "error",
        pid: null,
        startedAt: null,
        error: `${description} Automatic restart limit reached.`,
      });
      return;
    }
    this.update({
      status: "error",
      pid: null,
      startedAt: null,
      error: `${description} Restarting in ${retryDelay / 1_000} seconds.`,
    });
    this.retryTimer = setTimeout(() => void this.start(false), retryDelay);
  }

  private update(changes: Partial<LauncherState>): void {
    this.state = { ...this.state, ...changes };
    this.emit("state", this.snapshot());
  }

  private async log(source: LogSource, message: string): Promise<void> {
    const timestamp = new Date().toISOString();
    if (this.options.persistLog) {
      const entry = await this.options.persistLog({
        timestamp,
        source,
        message,
      });
      this.emit("log", entry);
      return;
    }
    this.emit("log", {
      id: `ephemeral-${timestamp}-${Math.random().toString(36).slice(2)}`,
      timestamp,
      source,
      message,
      beforeCursor: "",
      afterCursor: "",
    } satisfies PagedLogEntry);
  }

  private clearRetryTimer(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private clearStableTimer(): void {
    if (this.stableTimer) clearTimeout(this.stableTimer);
    this.stableTimer = null;
  }
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function terminateWindowsProcessTree(pid: number): Promise<void> {
  if (!Number.isInteger(pid) || pid <= 0) return Promise.resolve();
  return new Promise((resolve) => {
    execFile(
      "taskkill.exe",
      ["/PID", String(pid), "/T", "/F"],
      { windowsHide: true },
      () => resolve(),
    );
  });
}
