import { contextBridge, ipcRenderer } from "electron";
import type {
  BootstrapCredentials,
  EnvironmentKey,
  LauncherBridge,
  LauncherState,
  PagedLogEntry,
  ReadLogsRequest,
  LogPage,
  SaveConfigRequest,
} from "./shared";

function subscribe<T>(
  channel: string,
  callback: (value: T) => void,
): () => void {
  const listener = (_event: Electron.IpcRendererEvent, value: T) =>
    callback(value);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

const bridge: LauncherBridge = {
  initialize: () => ipcRenderer.invoke("launcher:initialize"),
  saveConfig: (request: SaveConfigRequest) =>
    ipcRenderer.invoke("launcher:save-config", request),
  resetDefaults: () => ipcRenderer.invoke("launcher:reset-defaults"),
  start: () => ipcRenderer.invoke("launcher:start"),
  stop: () => ipcRenderer.invoke("launcher:stop"),
  restart: () => ipcRenderer.invoke("launcher:restart"),
  quit: () => ipcRenderer.invoke("launcher:quit"),
  hideWindow: () => ipcRenderer.invoke("launcher:hide-window"),
  openDashboard: () => ipcRenderer.invoke("launcher:open-dashboard"),
  chooseDirectory: (key: EnvironmentKey) =>
    ipcRenderer.invoke("launcher:choose-directory", key),
  chooseExecutable: (key: EnvironmentKey) =>
    ipcRenderer.invoke("launcher:choose-executable", key),
  openDataFolder: () => ipcRenderer.invoke("launcher:open-data-folder"),
  openLogsFolder: () => ipcRenderer.invoke("launcher:open-logs-folder"),
  readLogs: (request: ReadLogsRequest): Promise<LogPage> =>
    ipcRenderer.invoke("launcher:read-logs", request),
  copyLogText: (text: string) =>
    ipcRenderer.invoke("launcher:copy-log-text", text),
  clearLogs: () => ipcRenderer.invoke("launcher:clear-logs"),
  exportLogs: () => ipcRenderer.invoke("launcher:export-logs"),
  copyAndAcknowledgeCredentials: () =>
    ipcRenderer.invoke("launcher:copy-and-acknowledge-credentials"),
  onState: (callback: (state: LauncherState) => void) =>
    subscribe("launcher:state", callback),
  onPersistedLog: (callback: (entry: PagedLogEntry) => void) =>
    subscribe("launcher:log", callback),
  onLogsReset: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on("launcher:logs-reset", listener);
    return () => ipcRenderer.removeListener("launcher:logs-reset", listener);
  },
  onCredentials: (callback: (credentials: BootstrapCredentials) => void) =>
    subscribe("launcher:credentials", callback),
};

contextBridge.exposeInMainWorld("zenstreamLauncher", bridge);
