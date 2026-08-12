import type {
  EditableConfig,
  LauncherBridge,
  LauncherState,
  LogEntry,
  LogPage,
  PagedLogEntry,
} from "../shared";

const config: EditableConfig = {
  environment: {
    ORCHESTRATOR_HOST: "127.0.0.1",
    ORCHESTRATOR_PORT: "9088",
    METADATA_PATH:
      "C:\\Users\\Example\\AppData\\Local\\ZenStream Orchestrator\\metadata",
    CORS_ORIGINS: "",
    TRUSTED_PROXY_IPS: "",
    ZENSTREAM_LOG_LEVEL: "INFO",
    FFMPEG_PATH: "",
    FFPROBE_PATH: "",
    MAX_TRANSCODES: "0",
    MAX_TRANSCODES_PER_USER: "0",
    PLAYBACK_SESSION_IDLE_TIMEOUT_SECONDS: "45",
    FOREGROUND_WORKERS: "16",
    METADATA_ROOT_WORKERS: "12",
    METADATA_FETCH_WORKERS: "12",
    METADATA_ASSET_WORKERS: "12",
    METADATA_PROVIDER_TIMEOUT_SECONDS: "20",
    METADATA_IMAGE_TIMEOUT_SECONDS: "20",
    METADATA_IMAGE_HOST_ALLOWLIST:
      "image.tmdb.org,media.themoviedb.org,artworks.thetvdb.com",
  },
  secretConfigured: true,
  startWithWindows: false,
};

let state: LauncherState = {
  status: "running",
  pid: 9_088,
  error: null,
  restartRequired: false,
  backendVersion: "0.4.1+main.24",
  dashboardUrl: "http://127.0.0.1:9088/web/",
  startedAt: new Date().toISOString(),
};

const initialLogs: LogEntry[] = [
  {
    id: "demo-1",
    timestamp: new Date().toISOString(),
    source: "launcher",
    message: "Starting backend from the dedicated launcher environment",
  },
  {
    id: "demo-2",
    timestamp: new Date().toISOString(),
    source: "stdout",
    message: "Application startup complete.",
  },
  {
    id: "demo-3",
    timestamp: new Date().toISOString(),
    source: "launcher",
    message: "Backend ready at http://127.0.0.1:9088/web/",
  },
];

const stateListeners = new Set<(value: LauncherState) => void>();

function setState(changes: Partial<LauncherState>): LauncherState {
  state = { ...state, ...changes };
  stateListeners.forEach((listener) => listener({ ...state }));
  return { ...state };
}

export function createDevelopmentBridge(): LauncherBridge {
  const page: LogPage = {
    entries: initialLogs.map((entry, index) => ({
      ...entry,
      beforeCursor: `demo-before-${index}`,
      afterCursor: `demo-after-${index}`,
    })),
    olderCursor: null,
    newerCursor: null,
    hasOlder: false,
    hasNewer: false,
    cursorExpired: false,
  };
  return {
    initialize: async () => ({
      config: structuredClone(config),
      state: { ...state },
      credentials: null,
    }),
    saveConfig: async (request) => ({
      config: {
        environment: { ...request.environment },
        secretConfigured: true,
        startWithWindows: request.startWithWindows,
      },
      state: request.restart
        ? setState({ status: "running", restartRequired: false })
        : setState({ restartRequired: true }),
    }),
    resetDefaults: async () => structuredClone(config),
    start: async () => setState({ status: "running", error: null }),
    stop: async () => setState({ status: "stopped", pid: null }),
    restart: async () =>
      setState({ status: "running", error: null, restartRequired: false }),
    quit: async () => undefined,
    hideWindow: async () => undefined,
    openDashboard: async () => undefined,
    chooseDirectory: async () => null,
    chooseExecutable: async () => null,
    openDataFolder: async () => undefined,
    openLogsFolder: async () => undefined,
    readLogs: async () => structuredClone(page),
    copyLogText: async () => undefined,
    clearLogs: async () => undefined,
    exportLogs: async () => false,
    copyAndAcknowledgeCredentials: async () => undefined,
    onState: (listener) => {
      stateListeners.add(listener);
      return () => stateListeners.delete(listener);
    },
    onPersistedLog: (_listener: (entry: PagedLogEntry) => void) => () =>
      undefined,
    onLogsReset: () => () => undefined,
    onCredentials: () => () => undefined,
  };
}
