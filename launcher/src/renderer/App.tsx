import {
  IconAlertTriangle,
  IconBrandWindows,
  IconCheck,
  IconChevronRight,
  IconClipboard,
  IconDeviceFloppy,
  IconDownload,
  IconExternalLink,
  IconFileCode,
  IconFolder,
  IconKey,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
  IconRotateClockwise,
  IconSearch,
  IconSettings,
  IconSquare,
  IconTerminal2,
  IconTrash,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useMemo, useRef, useState } from "react";
import iconUrl from "../../../assets/icons/icon.png";
import type {
  BootstrapCredentials,
  EditableConfig,
  EnvironmentConfig,
  EnvironmentKey,
  LauncherState,
  LifecycleStatus,
  LogEntry,
  LogSource,
  SecretChange,
} from "../shared";

type Tab = "configuration" | "logs";
type InputType = "text" | "number" | "select";

interface FieldDefinition {
  key: EnvironmentKey;
  label: string;
  description: string;
  type?: InputType;
  options?: string[];
  browse?: "directory" | "executable";
}

interface FieldGroup {
  title: string;
  description: string;
  fields: FieldDefinition[];
}

const groups: FieldGroup[] = [
  {
    title: "Server",
    description: "Network binding and browser access",
    fields: [
      {
        key: "ORCHESTRATOR_HOST",
        label: "Server host",
        description:
          "Use 127.0.0.1 for local-only access or 0.0.0.0 for your LAN.",
      },
      {
        key: "ORCHESTRATOR_PORT",
        label: "Server port",
        description: "Port used by both the API and administrator dashboard.",
        type: "number",
      },
      {
        key: "CORS_ORIGINS",
        label: "CORS origins",
        description:
          "Optional comma-separated HTTP(S) origins allowed to call the API.",
      },
      {
        key: "TRUSTED_PROXY_IPS",
        label: "Trusted proxy IPs",
        description: "Optional comma-separated reverse-proxy addresses.",
      },
      {
        key: "ZENSTREAM_LOG_LEVEL",
        label: "Log level",
        description: "Verbosity for backend console and rotating file logs.",
        type: "select",
        options: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
      },
    ],
  },
  {
    title: "Storage and security",
    description: "Persistent application data and authentication",
    fields: [
      {
        key: "METADATA_PATH",
        label: "Metadata path",
        description:
          "SQLite, artwork, portraits, trickplay, and rotating logs are stored here.",
        browse: "directory",
      },
    ],
  },
  {
    title: "Media tools and playback",
    description: "Bundled tools are used when override paths are blank",
    fields: [
      {
        key: "FFMPEG_PATH",
        label: "FFmpeg override",
        description:
          "Optional custom ffmpeg.exe; leave blank to use the bundled build.",
        browse: "executable",
      },
      {
        key: "FFPROBE_PATH",
        label: "FFprobe override",
        description:
          "Optional custom ffprobe.exe; leave blank to use the bundled build.",
        browse: "executable",
      },
      {
        key: "MAX_TRANSCODES",
        label: "Maximum transcodes",
        description:
          "Process-wide fallback before dashboard settings are saved; 0 is unlimited.",
        type: "number",
      },
      {
        key: "MAX_TRANSCODES_PER_USER",
        label: "Maximum transcodes per user",
        description:
          "Per-user fallback before dashboard settings are saved; 0 is unlimited.",
        type: "number",
      },
      {
        key: "PLAYBACK_SESSION_IDLE_TIMEOUT_SECONDS",
        label: "Playback idle timeout",
        description:
          "Seconds before abandoned FFmpeg sessions are reaped (15–3600).",
        type: "number",
      },
    ],
  },
  {
    title: "Metadata",
    description: "Provider requests and artwork safeguards",
    fields: [
      {
        key: "METADATA_PROVIDER_TIMEOUT_SECONDS",
        label: "Provider timeout",
        description: "Maximum metadata provider request duration in seconds.",
        type: "number",
      },
      {
        key: "METADATA_IMAGE_TIMEOUT_SECONDS",
        label: "Image timeout",
        description: "Maximum artwork download duration in seconds.",
        type: "number",
      },
      {
        key: "METADATA_IMAGE_HOST_ALLOWLIST",
        label: "Image host allowlist",
        description:
          "Comma-separated provider hosts allowed for artwork downloads.",
      },
    ],
  },
  {
    title: "Performance",
    description: "Bounded worker pools used by interactive and scheduled work",
    fields: [
      {
        key: "FOREGROUND_WORKERS",
        label: "Foreground workers",
        description:
          "Threads reserved for authenticated interactive work (2–32).",
        type: "number",
      },
      {
        key: "METADATA_ROOT_WORKERS",
        label: "Metadata root workers",
        description:
          "Concurrent movie or series roots processed across libraries (1–64).",
        type: "number",
      },
      {
        key: "METADATA_FETCH_WORKERS",
        label: "Metadata fetch workers",
        description: "Concurrent provider fetch operations (1–64).",
        type: "number",
      },
      {
        key: "METADATA_ASSET_WORKERS",
        label: "Metadata asset workers",
        description: "Concurrent artwork and portrait operations (1–64).",
        type: "number",
      },
    ],
  },
];

const emptyState: LauncherState = {
  status: "stopped",
  pid: null,
  error: null,
  restartRequired: false,
  backendVersion: null,
  dashboardUrl: "http://127.0.0.1:9088/web/",
  startedAt: null,
};

const statusLabels: Record<LifecycleStatus, string> = {
  stopped: "Stopped",
  starting: "Starting",
  running: "Running",
  stopping: "Stopping",
  restarting: "Restarting",
  error: "Needs attention",
};

export default function App() {
  const [tab, setTab] = useState<Tab>("configuration");
  const [config, setConfig] = useState<EditableConfig | null>(null);
  const [draft, setDraft] = useState<EnvironmentConfig | null>(null);
  const [state, setState] = useState<LauncherState>(emptyState);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [credentials, setCredentials] = useState<BootstrapCredentials | null>(
    null,
  );
  const [secretMode, setSecretMode] = useState<SecretChange["mode"]>("keep");
  const [secretValue, setSecretValue] = useState("");
  const [startWithWindows, setStartWithWindows] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" });
  }, [tab]);

  useEffect(() => {
    const stopState = window.zenstreamLauncher.onState(setState);
    const stopLog = window.zenstreamLauncher.onLog((entry) =>
      setLogs((current) => [...current, entry].slice(-10_000)),
    );
    const stopCredentials =
      window.zenstreamLauncher.onCredentials(setCredentials);
    void window.zenstreamLauncher
      .initialize()
      .then((initial) => {
        setConfig(initial.config);
        setDraft(initial.config.environment);
        setStartWithWindows(initial.config.startWithWindows);
        setState(initial.state);
        setLogs(initial.logs);
        setCredentials(initial.credentials);
      })
      .catch((reason) => setError(messageFor(reason)));
    return () => {
      stopState();
      stopLog();
      stopCredentials();
    };
  }, []);

  async function run(action: () => Promise<unknown>, success?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (success) setNotice(success);
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusy(false);
    }
  }

  async function save(restart: boolean) {
    if (!draft) return;
    const secret: SecretChange =
      secretMode === "replace"
        ? { mode: "replace", value: secretValue }
        : { mode: secretMode };
    await run(
      async () => {
        const result = await window.zenstreamLauncher.saveConfig({
          environment: draft,
          secret,
          startWithWindows,
          restart,
        });
        setConfig(result.config);
        setDraft(result.config.environment);
        setState(result.state);
        setSecretMode("keep");
        setSecretValue("");
      },
      restart
        ? "Configuration saved and restart requested."
        : "Configuration saved.",
    );
  }

  async function resetDefaults() {
    await run(async () => {
      const defaults = await window.zenstreamLauncher.resetDefaults();
      setDraft(defaults.environment);
      setStartWithWindows(false);
      setSecretMode("generate");
      setSecretValue("");
    }, "Defaults loaded. Save to apply them.");
  }

  async function choosePath(field: FieldDefinition) {
    const selected =
      field.browse === "directory"
        ? await window.zenstreamLauncher.chooseDirectory(field.key)
        : await window.zenstreamLauncher.chooseExecutable(field.key);
    if (selected) updateField(field.key, selected);
  }

  function updateField(key: EnvironmentKey, value: string) {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  }

  const changed = useMemo(
    () =>
      Boolean(
        config &&
        draft &&
        (JSON.stringify(config.environment) !== JSON.stringify(draft) ||
          config.startWithWindows !== startWithWindows ||
          secretMode !== "keep"),
      ),
    [config, draft, secretMode, startWithWindows],
  );

  if (!draft || !config) {
    return (
      <main className="loading-shell">
        <img src={iconUrl} alt="" />
        <div className="loading-spinner" />
        <p>Preparing ZenStream Orchestrator…</p>
        {error && <p className="inline-error">{error}</p>}
      </main>
    );
  }

  const active = ["starting", "running", "stopping", "restarting"].includes(
    state.status,
  );

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand-block">
          <img src={iconUrl} alt="" className="brand-icon" />
          <div>
            <p className="wordmark">ZENSTREAM</p>
            <div className="title-row">
              <h1>Orchestrator</h1>
              <span className="version">
                {state.backendVersion ? `v${state.backendVersion}` : "Windows"}
              </span>
            </div>
          </div>
        </div>
        <div className="header-actions">
          <button
            className="button secondary"
            disabled={state.status !== "running" || busy}
            onClick={() =>
              void run(() => window.zenstreamLauncher.openDashboard())
            }
          >
            <IconExternalLink size={17} />
            Open dashboard
          </button>
          {active ? (
            <button
              className="button secondary"
              disabled={busy || state.status === "stopping"}
              onClick={() => void run(() => window.zenstreamLauncher.stop())}
            >
              <IconSquare size={16} />
              Stop
            </button>
          ) : (
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void run(() => window.zenstreamLauncher.start())}
            >
              <IconPlayerPlay size={17} />
              Start
            </button>
          )}
          <button
            className="icon-button danger"
            aria-label="Quit launcher and stop Orchestrator"
            title="Quit launcher and stop Orchestrator"
            onClick={() => void window.zenstreamLauncher.quit()}
          >
            <IconX size={19} />
          </button>
        </div>
      </header>

      <section className="status-strip" aria-live="polite">
        <span className={`status-dot ${state.status}`} />
        <div>
          <strong>{statusLabels[state.status]}</strong>
          <span>
            {state.status === "running"
              ? `${state.dashboardUrl}${state.pid ? ` · PID ${state.pid}` : ""}`
              : state.error ||
                "The launcher remains available from the Windows notification area."}
          </span>
        </div>
        {state.restartRequired && (
          <button
            className="restart-notice"
            onClick={() => void run(() => window.zenstreamLauncher.restart())}
          >
            <IconRefresh size={16} /> Restart to apply configuration
          </button>
        )}
      </section>

      {credentials && (
        <section className="credential-banner" role="alert">
          <IconKey size={22} />
          <div>
            <strong>Save your root administrator credentials now</strong>
            <p>
              They are generated once and are not written to launcher settings
              or exported logs.
            </p>
            <div className="credential-values">
              <code>Username: {credentials.username}</code>
              <code>Password: {credentials.password}</code>
            </div>
          </div>
          <button
            className="button primary"
            onClick={() =>
              void navigator.clipboard
                .writeText(
                  `Username: ${credentials.username}\nPassword: ${credentials.password}`,
                )
                .then(async () => {
                  await window.zenstreamLauncher.acknowledgeCredentials();
                  setCredentials(null);
                  setNotice("Credentials copied. Store them somewhere safe.");
                })
            }
          >
            <IconClipboard size={17} /> Copy and dismiss
          </button>
        </section>
      )}

      {(error || notice) && (
        <div
          className={error ? "message error" : "message success"}
          role="status"
        >
          {error ? <IconAlertTriangle size={18} /> : <IconCheck size={18} />}
          <span>{error || notice}</span>
          <button
            aria-label="Dismiss message"
            onClick={() => (error ? setError(null) : setNotice(null))}
          >
            <IconX size={16} />
          </button>
        </div>
      )}

      <nav className="tabs" aria-label="Launcher sections">
        <button
          className={tab === "configuration" ? "active" : ""}
          onClick={() => setTab("configuration")}
        >
          <IconSettings size={18} /> Configuration{" "}
          {changed && <span className="unsaved-dot" />}
        </button>
        <button
          className={tab === "logs" ? "active" : ""}
          onClick={() => setTab("logs")}
        >
          <IconTerminal2 size={18} /> Logs{" "}
          <span className="tab-count">{logs.length.toLocaleString()}</span>
        </button>
      </nav>

      {tab === "configuration" ? (
        <ConfigurationView
          draft={draft}
          groups={groups}
          busy={busy}
          changed={changed}
          secretMode={secretMode}
          secretValue={secretValue}
          startWithWindows={startWithWindows}
          onField={updateField}
          onBrowse={(field) => void run(() => choosePath(field))}
          onSecretMode={setSecretMode}
          onSecretValue={setSecretValue}
          onStartWithWindows={setStartWithWindows}
          onOpenData={() =>
            void run(() => window.zenstreamLauncher.openDataFolder())
          }
          onOpenLogs={() =>
            void run(() => window.zenstreamLauncher.openLogsFolder())
          }
          onReset={() => void resetDefaults()}
          onSave={(restart) => void save(restart)}
        />
      ) : (
        <LogsView
          logs={logs}
          onClear={() => setLogs([])}
          onError={setError}
          onNotice={setNotice}
        />
      )}
    </div>
  );
}

function ConfigurationView({
  draft,
  groups,
  busy,
  changed,
  secretMode,
  secretValue,
  startWithWindows,
  onField,
  onBrowse,
  onSecretMode,
  onSecretValue,
  onStartWithWindows,
  onOpenData,
  onOpenLogs,
  onReset,
  onSave,
}: {
  draft: EnvironmentConfig;
  groups: FieldGroup[];
  busy: boolean;
  changed: boolean;
  secretMode: SecretChange["mode"];
  secretValue: string;
  startWithWindows: boolean;
  onField: (key: EnvironmentKey, value: string) => void;
  onBrowse: (field: FieldDefinition) => void;
  onSecretMode: (mode: SecretChange["mode"]) => void;
  onSecretValue: (value: string) => void;
  onStartWithWindows: (value: boolean) => void;
  onOpenData: () => void;
  onOpenLogs: () => void;
  onReset: () => void;
  onSave: (restart: boolean) => void;
}) {
  return (
    <main className="content configuration-content">
      <div className="content-heading">
        <div>
          <p className="kicker">Native Windows service</p>
          <h2>Configuration</h2>
          <p>
            Saved values are supplied directly to the Orchestrator process on
            its next start.
          </p>
        </div>
        <div className="toolbar">
          <button className="button ghost" disabled={busy} onClick={onReset}>
            <IconRotateClockwise size={17} /> Reset defaults
          </button>
          <button
            className="button secondary"
            disabled={busy || !changed}
            onClick={() => onSave(false)}
          >
            <IconDeviceFloppy size={17} /> Save
          </button>
          <button
            className="button primary"
            disabled={busy || !changed}
            onClick={() => onSave(true)}
          >
            <IconRefresh size={17} /> Save & restart
          </button>
        </div>
      </div>

      <section className="quick-actions" aria-label="Application folders">
        <button onClick={onOpenData}>
          <IconFolder size={19} />
          <span>
            <strong>Open data folder</strong>
            <small>Database and generated caches</small>
          </span>
          <IconChevronRight size={18} />
        </button>
        <button onClick={onOpenLogs}>
          <IconFileCode size={19} />
          <span>
            <strong>Open logs folder</strong>
            <small>Rotating backend diagnostics</small>
          </span>
          <IconChevronRight size={18} />
        </button>
      </section>

      {groups.map((group) => (
        <section className="settings-card" key={group.title}>
          <div className="settings-card-heading">
            <h3>{group.title}</h3>
            <p>{group.description}</p>
          </div>
          <div className="field-list">
            {group.fields.map((field) => (
              <label className="field-row" key={field.key}>
                <span className="field-copy">
                  <strong>{field.label}</strong>
                  <small>{field.description}</small>
                  <code>{field.key}</code>
                </span>
                <span className="input-with-action">
                  {field.type === "select" ? (
                    <select
                      value={draft[field.key]}
                      onChange={(event) =>
                        onField(field.key, event.target.value)
                      }
                    >
                      {field.options?.map((option) => (
                        <option key={option}>{option}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type={field.type || "text"}
                      value={draft[field.key]}
                      min={field.type === "number" ? 0 : undefined}
                      onChange={(event) =>
                        onField(field.key, event.target.value)
                      }
                      spellCheck={false}
                    />
                  )}
                  {field.browse && (
                    <button
                      type="button"
                      className="browse-button"
                      onClick={() => onBrowse(field)}
                      aria-label={`Browse for ${field.label}`}
                    >
                      <IconFolder size={18} />
                    </button>
                  )}
                </span>
              </label>
            ))}
          </div>
        </section>
      ))}

      <section className="settings-card">
        <div className="settings-card-heading">
          <h3>Launcher</h3>
          <p>Windows startup and protected application identity</p>
        </div>
        <div className="field-list">
          <div className="field-row secret-row">
            <span className="field-copy">
              <strong>Secret key</strong>
              <small>
                Encrypted with Windows secure storage. Replacing it revokes
                sessions and may invalidate saved provider credentials.
              </small>
              <code>SECRET_KEY</code>
            </span>
            <span className="secret-controls">
              <select
                value={secretMode}
                onChange={(event) =>
                  onSecretMode(event.target.value as SecretChange["mode"])
                }
              >
                <option value="keep">Keep protected key</option>
                <option value="generate">Generate a new key</option>
                <option value="replace">Enter a replacement</option>
              </select>
              {secretMode === "replace" && (
                <input
                  type="password"
                  value={secretValue}
                  onChange={(event) => onSecretValue(event.target.value)}
                  placeholder="At least 32 characters"
                  autoComplete="new-password"
                />
              )}
            </span>
          </div>
          <label className="field-row toggle-row">
            <span className="field-copy">
              <strong>Start with Windows</strong>
              <small>
                Launch hidden in the notification area and start Orchestrator
                after sign-in. Moving a portable executable breaks this
                registration.
              </small>
            </span>
            <span className="toggle-control">
              <IconBrandWindows size={18} />
              <input
                type="checkbox"
                checked={startWithWindows}
                onChange={(event) => onStartWithWindows(event.target.checked)}
              />
            </span>
          </label>
        </div>
      </section>
    </main>
  );
}

function LogsView({
  logs,
  onClear,
  onError,
  onNotice,
}: {
  logs: LogEntry[];
  onClear: () => void;
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<LogSource | "all">("all");
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [frozenLogs, setFrozenLogs] = useState<LogEntry[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const visibleBase = paused ? frozenLogs : logs;
  const filtered = useMemo(() => {
    const needle = query.toLowerCase();
    return visibleBase.filter(
      (entry) =>
        (source === "all" || entry.source === source) &&
        (!needle || entry.message.toLowerCase().includes(needle)),
    );
  }, [query, source, visibleBase]);

  useEffect(() => {
    if (autoScroll && !paused) endRef.current?.scrollIntoView({ block: "end" });
  }, [autoScroll, filtered.length, paused]);

  function togglePause() {
    if (!paused) setFrozenLogs(logs);
    setPaused((value) => !value);
  }

  async function copyVisible() {
    try {
      await navigator.clipboard.writeText(
        filtered
          .map(
            (entry) => `${entry.timestamp} [${entry.source}] ${entry.message}`,
          )
          .join("\n"),
      );
      onNotice("Visible log lines copied.");
    } catch (reason) {
      onError(messageFor(reason));
    }
  }

  return (
    <main className="content logs-content">
      <div className="content-heading">
        <div>
          <p className="kicker">Live process output</p>
          <h2>Logs</h2>
          <p>
            stdout, stderr, and launcher lifecycle messages are kept in memory
            for this session.
          </p>
        </div>
        <div className="toolbar">
          <button
            className={`button ghost ${paused ? "selected" : ""}`}
            onClick={togglePause}
          >
            {paused ? (
              <IconPlayerPlay size={17} />
            ) : (
              <IconPlayerPause size={17} />
            )}
            {paused ? "Resume view" : "Pause view"}
          </button>
          <button className="button ghost" onClick={() => void copyVisible()}>
            <IconClipboard size={17} /> Copy visible
          </button>
          <button
            className="button secondary"
            onClick={() =>
              void window.zenstreamLauncher
                .exportLogs()
                .then(
                  (saved) =>
                    saved &&
                    onNotice(
                      "Logs exported with bootstrap passwords redacted.",
                    ),
                )
                .catch((reason) => onError(messageFor(reason)))
            }
          >
            <IconDownload size={17} /> Export
          </button>
          <button
            className="icon-button"
            aria-label="Clear in-memory logs"
            title="Clear in-memory logs"
            onClick={() => {
              void window.zenstreamLauncher.clearLogs();
              onClear();
              setFrozenLogs([]);
              onNotice("In-memory launcher logs cleared.");
            }}
          >
            <IconTrash size={18} />
          </button>
        </div>
      </div>
      <div className="log-filters">
        <label className="search-box">
          <IconSearch size={17} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search output"
          />
        </label>
        <select
          value={source}
          onChange={(event) =>
            setSource(event.target.value as LogSource | "all")
          }
        >
          <option value="all">All sources</option>
          <option value="launcher">Launcher</option>
          <option value="stdout">stdout</option>
          <option value="stderr">stderr</option>
        </select>
        <label className="auto-scroll-toggle">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(event) => setAutoScroll(event.target.checked)}
          />
          Follow output
        </label>
        <span className="log-count">
          {filtered.length.toLocaleString()} lines
        </span>
      </div>
      <section
        className="log-console"
        aria-label="Backend process output"
        aria-live={paused ? "off" : "polite"}
      >
        {filtered.length === 0 ? (
          <div className="empty-logs">
            <IconTerminal2 size={28} />
            <p>No matching output.</p>
          </div>
        ) : (
          filtered.map((entry) => (
            <div className={`log-line ${entry.source}`} key={entry.id}>
              <time>{new Date(entry.timestamp).toLocaleTimeString()}</time>
              <span className="log-source">{entry.source}</span>
              <span>{entry.message || " "}</span>
            </div>
          ))
        )}
        <div ref={endRef} />
      </section>
    </main>
  );
}

function messageFor(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
