export type LifecycleStatus =
  "stopped" | "starting" | "running" | "stopping" | "restarting" | "error";

export type LogSource = "stdout" | "stderr" | "launcher";

export interface LogEntry {
  id: string;
  timestamp: string;
  source: LogSource;
  message: string;
}

export interface PagedLogEntry extends LogEntry {
  beforeCursor: LogCursor;
  afterCursor: LogCursor;
}

export type LogCursor = string;

export interface ReadLogsRequest {
  direction: "older" | "newer";
  cursor?: LogCursor;
  limit?: number;
  source?: LogSource | "all";
  query?: string;
}

export interface LogPage {
  entries: PagedLogEntry[];
  olderCursor: LogCursor | null;
  newerCursor: LogCursor | null;
  hasOlder: boolean;
  hasNewer: boolean;
  cursorExpired: boolean;
}

export interface BootstrapCredentials {
  username: string;
  password: string;
}

export interface LauncherState {
  status: LifecycleStatus;
  pid: number | null;
  error: string | null;
  restartRequired: boolean;
  backendVersion: string | null;
  dashboardUrl: string;
  startedAt: string | null;
}

export const ENVIRONMENT_KEYS = [
  "ORCHESTRATOR_HOST",
  "ORCHESTRATOR_PORT",
  "ZENSTREAM_PUBLIC_WEB_URL",
  "METADATA_PATH",
  "CORS_ORIGINS",
  "TRUSTED_PROXY_IPS",
  "ZENSTREAM_LOG_LEVEL",
  "FFMPEG_PATH",
  "FFPROBE_PATH",
  "MAX_TRANSCODES",
  "MAX_TRANSCODES_PER_USER",
  "PLAYBACK_SESSION_IDLE_TIMEOUT_SECONDS",
  "FOREGROUND_WORKERS",
  "METADATA_ROOT_WORKERS",
  "METADATA_FETCH_WORKERS",
  "METADATA_ASSET_WORKERS",
  "METADATA_PROVIDER_TIMEOUT_SECONDS",
  "METADATA_IMAGE_TIMEOUT_SECONDS",
  "METADATA_IMAGE_HOST_ALLOWLIST",
] as const;

export type EnvironmentKey = (typeof ENVIRONMENT_KEYS)[number];
export type EnvironmentConfig = Record<EnvironmentKey, string>;

export interface EditableConfig {
  environment: EnvironmentConfig;
  secretConfigured: boolean;
  startWithWindows: boolean;
}

export type SecretChange =
  { mode: "keep" } | { mode: "generate" } | { mode: "replace"; value: string };

export interface SaveConfigRequest {
  environment: EnvironmentConfig;
  secret: SecretChange;
  startWithWindows: boolean;
  restart: boolean;
}

export interface SaveConfigResult {
  config: EditableConfig;
  state: LauncherState;
}

export interface LauncherBridge {
  initialize(): Promise<{
    config: EditableConfig;
    state: LauncherState;
    credentials: BootstrapCredentials | null;
  }>;
  saveConfig(request: SaveConfigRequest): Promise<SaveConfigResult>;
  resetDefaults(): Promise<EditableConfig>;
  start(): Promise<LauncherState>;
  stop(): Promise<LauncherState>;
  restart(): Promise<LauncherState>;
  quit(): Promise<void>;
  hideWindow(): Promise<void>;
  openDashboard(): Promise<void>;
  chooseDirectory(key: EnvironmentKey): Promise<string | null>;
  chooseExecutable(key: EnvironmentKey): Promise<string | null>;
  openDataFolder(): Promise<void>;
  openLogsFolder(): Promise<void>;
  readLogs(request: ReadLogsRequest): Promise<LogPage>;
  copyLogText(text: string): Promise<void>;
  clearLogs(): Promise<void>;
  exportLogs(): Promise<boolean>;
  copyAndAcknowledgeCredentials(): Promise<void>;
  onState(callback: (state: LauncherState) => void): () => void;
  onPersistedLog(callback: (entry: PagedLogEntry) => void): () => void;
  onLogsReset(callback: () => void): () => void;
  onCredentials(
    callback: (credentials: BootstrapCredentials) => void,
  ): () => void;
}
