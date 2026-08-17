import {
  access,
  cp,
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  Menu,
  safeStorage,
  session,
  shell,
  Tray,
  type IpcMainInvokeEvent,
} from "electron";
import type {
  BootstrapCredentials,
  EnvironmentKey,
  LauncherState,
  LogEntry,
  ReadLogsRequest,
  SaveConfigRequest,
} from "../shared";
import { ConfigStore } from "./config-store";
import { LauncherLogStore } from "./log-store";
import { BackendSupervisor, type BackendCommand } from "./supervisor";

const configStore = new ConfigStore();
let logStore: LauncherLogStore | null = null;
let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let pendingCredentials: BootstrapCredentials | null = null;
let quitRequested = false;
let exitReady = false;
let packagedBackendCommand: BackendCommand | null = null;

const supervisor = new BackendSupervisor({
  resolveCommand: resolveBackendCommand,
  getEnvironment: () => configStore.environment(),
  persistLog: async (entry) => {
    if (!logStore) throw new Error("Launcher log store is not ready.");
    return logStore.append(entry);
  },
});

function resolveBackendCommand(): BackendCommand {
  if (app.isPackaged) {
    if (!packagedBackendCommand)
      throw new Error("Packaged Python environment is not ready.");
    return packagedBackendCommand;
  }
  const repository = path.resolve(__dirname, "../../..");
  const explicitBackend = process.env.ZENSTREAM_BACKEND_EXECUTABLE;
  if (explicitBackend) {
    return {
      command: explicitBackend,
      args: [],
      cwd: path.dirname(explicitBackend),
    };
  }
  return {
    command: process.env.ZENSTREAM_PYTHON || "python",
    args: [path.join(repository, "orchestrator", "launcher_entry.py")],
    cwd: repository,
  };
}

async function preparePackagedBackend(): Promise<void> {
  if (!app.isPackaged) return;
  const packagedRoot = path.join(process.resourcesPath, "backend");
  const sourceRoot = path.join(packagedRoot, "source");
  const pythonBase = path.join(packagedRoot, "python-base");
  const template = path.join(packagedRoot, "venv-template");
  const runtimeRoot = path.resolve(app.getPath("userData"), "runtime");
  const versionRoot = path.resolve(runtimeRoot, app.getVersion());
  const runtimeVenv = path.resolve(versionRoot, "venv");
  const preparingVenv = path.resolve(versionRoot, ".venv-preparing");
  const relative = path.relative(runtimeRoot, runtimeVenv);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(
      "Refusing to prepare a Python environment outside launcher runtime data.",
    );
  }
  const version = (
    await readFile(path.join(packagedRoot, "runtime-version.txt"), "utf8")
  ).trim();
  const pythonExecutable = path.join(pythonBase, "python.exe");
  const venvConfiguration = [
    `home = ${pythonBase}`,
    "include-system-site-packages = false",
    `version = ${version}`,
    `executable = ${pythonExecutable}`,
    `command = ${pythonExecutable} -m venv ${runtimeVenv}`,
    "",
  ].join("\n");
  try {
    await access(path.join(runtimeVenv, "Scripts", "python.exe"));
  } catch {
    await rm(runtimeVenv, { recursive: true, force: true });
    await rm(preparingVenv, { recursive: true, force: true });
    await mkdir(versionRoot, { recursive: true });
    try {
      await cp(template, preparingVenv, { recursive: true, force: true });
      await writeFile(
        path.join(preparingVenv, "pyvenv.cfg"),
        venvConfiguration,
        "utf8",
      );
      await rename(preparingVenv, runtimeVenv);
    } catch (error) {
      await rm(preparingVenv, { recursive: true, force: true });
      throw error;
    }
  }
  await writeFile(
    path.join(runtimeVenv, "pyvenv.cfg"),
    venvConfiguration,
    "utf8",
  );
  packagedBackendCommand = {
    command: path.join(runtimeVenv, "Scripts", "python.exe"),
    args: [path.join(sourceRoot, "orchestrator", "launcher_entry.py")],
    cwd: sourceRoot,
  };
}

function iconPath(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "assets", "icon.png")
    : path.resolve(__dirname, "../../../assets/icons/icon.png");
}

function send(channel: string, value: unknown): void {
  if (mainWindow && !mainWindow.isDestroyed())
    mainWindow.webContents.send(channel, value);
}

supervisor.on("log", (entry: LogEntry) => send("launcher:log", entry));
supervisor.on("log-error", (error) =>
  console.error("Could not persist launcher log", error),
);
supervisor.on("state", (state: LauncherState) => {
  send("launcher:state", state);
  rebuildTray(state);
});
supervisor.on("credentials", (credentials: BootstrapCredentials) => {
  pendingCredentials = credentials;
  send("launcher:credentials", credentials);
  showWindow();
});

function assertTrustedSender(event: IpcMainInvokeEvent): void {
  const url = event.senderFrame?.url || "";
  const isMainFrame =
    mainWindow &&
    event.sender === mainWindow.webContents &&
    event.senderFrame === mainWindow.webContents.mainFrame;
  const packagedUrl = pathToFileURL(
    path.join(__dirname, "../../dist/index.html"),
  ).href;
  const trusted = app.isPackaged
    ? url === packagedUrl
    : url.startsWith("http://127.0.0.1:5173");
  if (!isMainFrame || !trusted)
    throw new Error("Rejected IPC request from an untrusted renderer.");
}

function registerHandler(
  channel: string,
  handler: (event: IpcMainInvokeEvent, ...args: never[]) => unknown,
): void {
  ipcMain.handle(channel, async (event, ...args) => {
    assertTrustedSender(event);
    return handler(event, ...(args as never[]));
  });
}

function configureLoginStartup(enabled: boolean): void {
  app.setLoginItemSettings({
    openAtLogin: enabled,
    path: process.execPath,
    args: ["--hidden"],
  });
}

function registerIpc(): void {
  registerHandler("launcher:initialize", () => ({
    config: configStore.view(),
    state: supervisor.snapshot(),
    credentials: pendingCredentials,
  }));
  registerHandler("launcher:reset-defaults", () => configStore.defaults());
  registerHandler(
    "launcher:save-config",
    async (_event, request: SaveConfigRequest) => {
      const before = configStore.view();
      const config = await configStore.save(request);
      configureLoginStartup(config.startWithWindows);
      if (
        logStore &&
        before.environment.METADATA_PATH !== config.environment.METADATA_PATH
      ) {
        await logStore.setDirectory(
          path.join(config.environment.METADATA_PATH, "logs"),
        );
        send("launcher:logs-reset", undefined);
      }
      const changed =
        JSON.stringify(before.environment) !==
        JSON.stringify(config.environment);
      const requiresRestart = changed || request.secret.mode !== "keep";
      if (requiresRestart && supervisor.snapshot().status === "running") {
        supervisor.setRestartRequired(true);
      }
      const state = request.restart
        ? await supervisor.restart()
        : supervisor.snapshot();
      rebuildTray(state);
      return { config, state };
    },
  );
  registerHandler("launcher:start", () => supervisor.start());
  registerHandler("launcher:stop", () => supervisor.stop());
  registerHandler("launcher:restart", () => supervisor.restart());
  registerHandler("launcher:quit", async () => requestQuit());
  registerHandler("launcher:hide-window", () => {
    mainWindow?.hide();
  });
  registerHandler("launcher:open-dashboard", async () => {
    if (supervisor.snapshot().status !== "running") {
      throw new Error(
        "The dashboard is available after Orchestrator is running.",
      );
    }
    await shell.openExternal(supervisor.snapshot().dashboardUrl);
  });
  registerHandler(
    "launcher:choose-directory",
    async (_event, key: EnvironmentKey) => {
      if (key !== "METADATA_PATH")
        throw new Error("Unsupported directory setting.");
      const result = await dialog.showOpenDialog(mainWindow!, {
        properties: ["openDirectory", "createDirectory"],
      });
      return result.canceled ? null : result.filePaths[0] || null;
    },
  );
  registerHandler(
    "launcher:choose-executable",
    async (_event, key: EnvironmentKey) => {
      if (
        !(["FFMPEG_PATH", "FFPROBE_PATH"] as EnvironmentKey[]).includes(key)
      ) {
        throw new Error("Unsupported executable setting.");
      }
      const result = await dialog.showOpenDialog(mainWindow!, {
        properties: ["openFile"],
        filters: [{ name: "Windows executable", extensions: ["exe"] }],
      });
      return result.canceled ? null : result.filePaths[0] || null;
    },
  );
  registerHandler("launcher:open-data-folder", async () => {
    const directory = configStore.view().environment.METADATA_PATH;
    await mkdir(directory, { recursive: true });
    const error = await shell.openPath(directory);
    if (error) throw new Error(error);
  });
  registerHandler("launcher:open-logs-folder", async () => {
    const directory = path.join(
      configStore.view().environment.METADATA_PATH,
      "logs",
    );
    await mkdir(directory, { recursive: true });
    const error = await shell.openPath(directory);
    if (error) throw new Error(error);
  });
  registerHandler(
    "launcher:read-logs",
    async (_event, request: ReadLogsRequest) => {
      if (!logStore) throw new Error("Launcher log store is not ready.");
      return logStore.read(request);
    },
  );
  registerHandler("launcher:copy-log-text", (_event, text: string) => {
    if (
      typeof text !== "string" ||
      Buffer.byteLength(text, "utf8") > 2 * 1024 * 1024
    )
      throw new Error("The selected log text is too large to copy at once.");
    clipboard.writeText(text);
  });
  registerHandler("launcher:clear-logs", async () => {
    if (!logStore) return;
    await logStore.clear();
    send("launcher:logs-reset", undefined);
  });
  registerHandler("launcher:export-logs", async () => {
    const result = await dialog.showSaveDialog(mainWindow!, {
      defaultPath: `zenstream-launcher-${new Date().toISOString().slice(0, 10)}.log`,
      filters: [{ name: "Log file", extensions: ["log", "txt"] }],
    });
    if (result.canceled || !result.filePath) return false;
    if (!logStore) throw new Error("Launcher log store is not ready.");
    await logStore.exportTo(result.filePath);
    return true;
  });
  registerHandler("launcher:copy-and-acknowledge-credentials", () => {
    if (!pendingCredentials)
      throw new Error("No bootstrap credentials are waiting to be copied.");
    clipboard.writeText(
      `Username: ${pendingCredentials.username}\nPassword: ${pendingCredentials.password}`,
    );
    pendingCredentials = null;
  });
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1_120,
    height: 760,
    minWidth: 820,
    minHeight: 600,
    show: false,
    backgroundColor: "#000000",
    icon: iconPath(),
    title: "ZenStream Orchestrator",
    webPreferences: {
      preload: path.join(__dirname, "../preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.removeMenu();
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  mainWindow.on("close", (event) => {
    if (!exitReady) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  if (process.env.VITE_DEV_SERVER_URL) {
    void mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    void mainWindow.loadFile(path.join(__dirname, "../../dist/index.html"));
  }
  mainWindow.once("ready-to-show", () => {
    if (!process.argv.includes("--hidden")) mainWindow?.show();
  });
}

function showWindow(): void {
  if (!mainWindow) createWindow();
  mainWindow?.show();
  if (mainWindow?.isMinimized()) mainWindow.restore();
  mainWindow?.focus();
}

function rebuildTray(state = supervisor.snapshot()): void {
  if (!tray) return;
  const running = state.status === "running";
  const active = ["starting", "running", "restarting", "stopping"].includes(
    state.status,
  );
  const startup = configStore.view().startWithWindows;
  tray.setToolTip(`ZenStream Orchestrator — ${state.status}`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: `Status: ${state.status}`, enabled: false },
      { type: "separator" },
      { label: "Show Launcher", click: showWindow },
      {
        label: "Open Dashboard",
        enabled: running,
        click: () => void shell.openExternal(state.dashboardUrl),
      },
      { type: "separator" },
      {
        label: "Start",
        enabled: !active,
        click: () => void supervisor.start(),
      },
      {
        label: "Restart",
        enabled: active,
        click: () => void supervisor.restart(),
      },
      { label: "Stop", enabled: active, click: () => void supervisor.stop() },
      { type: "separator" },
      {
        label: "Start with Windows",
        type: "checkbox",
        checked: startup,
        click: (item) => {
          const current = configStore.view();
          void configStore
            .save({
              environment: current.environment,
              secret: { mode: "keep" },
              startWithWindows: item.checked,
              restart: false,
            })
            .then(() => {
              configureLoginStartup(item.checked);
              rebuildTray();
              send("launcher:state", supervisor.snapshot());
            });
        },
      },
      { type: "separator" },
      { label: "Quit", click: () => void requestQuit() },
    ]),
  );
}

async function requestQuit(): Promise<void> {
  if (quitRequested) return;
  quitRequested = true;
  mainWindow?.hide();
  await supervisor.stop();
  await logStore?.close();
  exitReady = true;
  tray?.destroy();
  tray = null;
  app.quit();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);
  app.on("before-quit", (event) => {
    if (!exitReady) {
      event.preventDefault();
      void requestQuit();
    }
  });
  app.on("window-all-closed", () => undefined);
  void app
    .whenReady()
    .then(async () => {
      if (!safeStorage.isEncryptionAvailable()) {
        throw new Error(
          "ZenStream Orchestrator cannot protect SECRET_KEY with Windows secure storage.",
        );
      }
      await configStore.load();
      logStore = new LauncherLogStore(
        path.join(configStore.view().environment.METADATA_PATH, "logs"),
      );
      await preparePackagedBackend();
      configureLoginStartup(configStore.view().startWithWindows);
      session.defaultSession.setPermissionRequestHandler(
        (_webContents, _permission, callback) => callback(false),
      );
      registerIpc();
      createWindow();
      tray = new Tray(iconPath());
      tray.on("click", showWindow);
      rebuildTray();
      await supervisor.start();
    })
    .catch((error) => {
      dialog.showErrorBox("Launcher startup failed", String(error));
      exitReady = true;
      app.quit();
    });
}
