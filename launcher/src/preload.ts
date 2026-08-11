import { contextBridge, ipcRenderer } from "electron";
import type {
  BootstrapCredentials,
  EnvironmentKey,
  LauncherBridge,
  LauncherState,
  LogEntry,
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
  openDashboard: () => ipcRenderer.invoke("launcher:open-dashboard"),
  chooseDirectory: (key: EnvironmentKey) =>
    ipcRenderer.invoke("launcher:choose-directory", key),
  chooseExecutable: (key: EnvironmentKey) =>
    ipcRenderer.invoke("launcher:choose-executable", key),
  openDataFolder: () => ipcRenderer.invoke("launcher:open-data-folder"),
  openLogsFolder: () => ipcRenderer.invoke("launcher:open-logs-folder"),
  clearLogs: () => ipcRenderer.invoke("launcher:clear-logs"),
  exportLogs: () => ipcRenderer.invoke("launcher:export-logs"),
  acknowledgeCredentials: () =>
    ipcRenderer.invoke("launcher:acknowledge-credentials"),
  onState: (callback: (state: LauncherState) => void) =>
    subscribe("launcher:state", callback),
  onLog: (callback: (entry: LogEntry) => void) =>
    subscribe("launcher:log", callback),
  onCredentials: (callback: (credentials: BootstrapCredentials) => void) =>
    subscribe("launcher:credentials", callback),
};

contextBridge.exposeInMainWorld("zenstreamLauncher", bridge);
