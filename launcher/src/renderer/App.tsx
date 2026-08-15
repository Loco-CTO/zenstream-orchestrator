import {
  IconAlertTriangle,
  IconArrowDown,
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
  LogPage,
  PagedLogEntry,
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
        key: "ZENSTREAM_PUBLIC_WEB_URL",
        label: "Public web URL",
        description:
          "HTTP(S) origin of the public ZenStream web client used for invite links.",
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
        key: "CONTROL_WORKERS",
        label: "Control workers",
        description:
          "Threads for bounded SyncPlay, administrator, library, metadata, and filesystem work (1–64).",
        type: "number",
      },
      {
        key: "AUTH_WORKERS",
        label: "Authentication workers",
        description:
          "Threads for password, session, ticket, and administrator identity work (1–16).",
        type: "number",
      },
      {
        key: "CONTROL_QUEUE",
        label: "Control queue",
        description:
          "Additional pending control-work slots; 0 disables pending slots (0–256).",
        type: "number",
      },
      {
        key: "AUTH_QUEUE",
        label: "Authentication queue",
        description:
          "Additional pending authentication-work slots; 0 disables pending slots (0–128).",
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
  const [unseenLogs, setUnseenLogs] = useState(0);
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
    const stopCredentials =
      window.zenstreamLauncher.onCredentials(setCredentials);
    void window.zenstreamLauncher
      .initialize()
      .then((initial) => {
        setConfig(initial.config);
        setDraft(initial.config.environment);
        setStartWithWindows(initial.config.startWithWindows);
        setState(initial.state);
        setCredentials(initial.credentials);
      })
      .catch((reason) => setError(messageFor(reason)));
    return () => {
      stopState();
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
            aria-label="Close launcher window"
            title="Close launcher window; Orchestrator remains running in the tray"
            onClick={() => void window.zenstreamLauncher.hideWindow()}
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
              <label className="credential-value">
                <span>Username</span>
                <input
                  readOnly
                  value={credentials.username}
                  aria-label="Bootstrap username"
                />
              </label>
              <label className="credential-value">
                <span>Password</span>
                <input
                  readOnly
                  value={credentials.password}
                  aria-label="Bootstrap password"
                />
              </label>
            </div>
          </div>
          <button
            className="button primary"
            onClick={() =>
              void run(async () => {
                await window.zenstreamLauncher.copyAndAcknowledgeCredentials();
                setCredentials(null);
              }, "Credentials copied. Store them somewhere safe.")
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
          {unseenLogs > 0 && (
            <span className="tab-count">{unseenLogs.toLocaleString()}</span>
          )}
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
          onUnseenChange={setUnseenLogs}
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
  onUnseenChange,
  onError,
  onNotice,
}: {
  onUnseenChange: (count: number) => void;
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<LogSource | "all">("all");
  const [entries, setEntries] = useState<PagedLogEntry[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [newerCursor, setNewerCursor] = useState<string | null>(null);
  const [hasOlder, setHasOlder] = useState(false);
  const [hasNewer, setHasNewer] = useState(false);
  const [paused, setPaused] = useState(false);
  const [follow, setFollow] = useState(true);
  const [unseen, setUnseen] = useState(0);
  const [loading, setLoading] = useState(false);
  const consoleRef = useRef<HTMLElement>(null);
  const requestGeneration = useRef(0);

  useEffect(() => onUnseenChange(unseen), [onUnseenChange, unseen]);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    const timer = window.setTimeout(() => {
      setLoading(true);
      void window.zenstreamLauncher
        .readLogs({ direction: "older", limit: 250, source, query })
        .then((page) => {
          if (generation !== requestGeneration.current) return;
          applyPage(
            page,
            setEntries,
            setOlderCursor,
            setNewerCursor,
            setHasOlder,
            setHasNewer,
          );
          setUnseen(0);
          requestAnimationFrame(() => {
            if (consoleRef.current)
              consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
          });
        })
        .catch((reason) => onError(messageFor(reason)))
        .finally(() => setLoading(false));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [onError, query, source]);

  useEffect(() => {
    const stopLog = window.zenstreamLauncher.onPersistedLog((entry) => {
      if (source !== "all" && entry.source !== source) return;
      if (query && !entry.message.toLowerCase().includes(query.toLowerCase()))
        return;
      if (paused || !follow) {
        setUnseen((value) => value + 1);
        return;
      }
      setEntries((current) => appendBounded(current, entry));
      setNewerCursor(entry.afterCursor);
      requestAnimationFrame(() => {
        if (consoleRef.current)
          consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
      });
    });
    const stopReset = window.zenstreamLauncher.onLogsReset(() => {
      setEntries([]);
      setOlderCursor(null);
      setNewerCursor(null);
      setHasOlder(false);
      setHasNewer(false);
      setUnseen(0);
      onNotice("Launcher log history was reset.");
    });
    return () => {
      stopLog();
      stopReset();
    };
  }, [follow, onNotice, paused, query, source]);

  async function loadOlder() {
    if (loading || !hasOlder || !olderCursor || !consoleRef.current) return;
    const consoleElement = consoleRef.current;
    const oldHeight = consoleElement.scrollHeight;
    const generation = requestGeneration.current;
    setLoading(true);
    try {
      const page = await window.zenstreamLauncher.readLogs({
        direction: "older",
        cursor: olderCursor,
        limit: 250,
        source,
        query,
      });
      if (generation !== requestGeneration.current) return;
      if (page.cursorExpired) {
        onNotice(
          "Older launcher log history has rotated out; showing the latest output.",
        );
        await scrollToLatest();
        return;
      }
      setEntries((current) => prependBounded(current, page.entries));
      setOlderCursor(page.olderCursor);
      setNewerCursor((current) => current ?? page.newerCursor);
      setHasOlder(page.hasOlder);
      setHasNewer(true);
      requestAnimationFrame(() => {
        consoleElement.scrollTop += consoleElement.scrollHeight - oldHeight;
      });
    } catch (reason) {
      onError(messageFor(reason));
    } finally {
      setLoading(false);
    }
  }

  async function loadNewer() {
    if (loading || !hasNewer || !newerCursor) return;
    const generation = requestGeneration.current;
    setLoading(true);
    try {
      const page = await window.zenstreamLauncher.readLogs({
        direction: "newer",
        cursor: newerCursor,
        limit: 250,
        source,
        query,
      });
      if (generation !== requestGeneration.current) return;
      if (page.cursorExpired) {
        onNotice(
          "Newer launcher log history has rotated out; showing the latest output.",
        );
        await scrollToLatest();
        return;
      }
      setEntries((current) => appendManyBounded(current, page.entries));
      setOlderCursor((current) => current ?? page.olderCursor);
      setNewerCursor(page.newerCursor);
      setHasOlder(true);
      setHasNewer(page.hasNewer);
    } catch (reason) {
      onError(messageFor(reason));
    } finally {
      setLoading(false);
    }
  }

  async function scrollToLatest() {
    try {
      const page = await window.zenstreamLauncher.readLogs({
        direction: "older",
        limit: 250,
        source,
        query,
      });
      setEntries(page.entries);
      setOlderCursor(page.olderCursor);
      setNewerCursor(page.newerCursor);
      setHasOlder(page.hasOlder);
      setHasNewer(page.hasNewer);
      setUnseen(0);
      if (!paused) setFollow(true);
      requestAnimationFrame(() => {
        if (consoleRef.current)
          consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
      });
    } catch (reason) {
      onError(messageFor(reason));
    }
  }

  async function copyLoaded() {
    try {
      await window.zenstreamLauncher.copyLogText(
        entries
          .map(
            (entry) => `${entry.timestamp} [${entry.source}] ${entry.message}`,
          )
          .join("\n"),
      );
      onNotice("Loaded log lines copied.");
    } catch (reason) {
      onError(messageFor(reason));
    }
  }

  function handleScroll() {
    const element = consoleRef.current;
    if (!element) return;
    const atBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 48;
    if (!atBottom) setFollow(false);
    if (element.scrollTop < 48) void loadOlder();
    if (atBottom) void loadNewer();
  }

  function togglePause() {
    if (paused) {
      setPaused(false);
      setFollow(true);
      void scrollToLatest();
    } else {
      setPaused(true);
    }
  }

  return (
    <main className="content logs-content">
      <div className="content-heading">
        <div>
          <p className="kicker">Live process output</p>
          <h2>Logs</h2>
          <p>
            stdout, stderr, and launcher lifecycle messages are loaded from disk
            as you browse.
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
          <button className="button ghost" onClick={() => void copyLoaded()}>
            <IconClipboard size={17} /> Copy loaded
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
            aria-label="Clear launcher logs"
            title="Clear launcher logs"
            onClick={() =>
              void window.zenstreamLauncher
                .clearLogs()
                .catch((reason) => onError(messageFor(reason)))
            }
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
            checked={follow}
            onChange={(event) => setFollow(event.target.checked)}
          />
          Follow output
        </label>
        <span className="log-count">
          {entries.length.toLocaleString()} loaded
          {unseen ? ` · ${unseen.toLocaleString()} new` : ""}
        </span>
      </div>
      <div className="log-console-shell">
        <section
          ref={consoleRef}
          className="log-console"
          aria-label="Backend process output"
          aria-live={paused ? "off" : "polite"}
          onScroll={handleScroll}
        >
          {entries.length === 0 ? (
            <div className="empty-logs">
              <IconTerminal2 size={28} />
              <p>No matching output.</p>
            </div>
          ) : (
            entries.map((entry) => (
              <div className={`log-line ${entry.source}`} key={entry.id}>
                <time>{new Date(entry.timestamp).toLocaleTimeString()}</time>
                <span className="log-source">{entry.source}</span>
                <span>{entry.message || " "}</span>
              </div>
            ))
          )}
        </section>
        {(unseen > 0 || !follow || hasNewer) && (
          <button
            className="scroll-latest"
            aria-label="Scroll to latest logs"
            title="Scroll to latest logs"
            onClick={() => void scrollToLatest()}
          >
            <IconArrowDown size={18} />
          </button>
        )}
      </div>
    </main>
  );
}

function applyPage(
  page: LogPage,
  setEntries: (value: PagedLogEntry[]) => void,
  setOlderCursor: (value: string | null) => void,
  setNewerCursor: (value: string | null) => void,
  setHasOlder: (value: boolean) => void,
  setHasNewer: (value: boolean) => void,
): void {
  setEntries(page.entries);
  setOlderCursor(page.olderCursor);
  setNewerCursor(page.newerCursor);
  setHasOlder(page.hasOlder);
  setHasNewer(page.hasNewer);
}

function appendBounded(
  current: PagedLogEntry[],
  entry: PagedLogEntry,
): PagedLogEntry[] {
  return appendManyBounded(current, [entry]);
}

function appendManyBounded(
  current: PagedLogEntry[],
  additions: PagedLogEntry[],
): PagedLogEntry[] {
  const seen = new Set(current.map((entry) => entry.id));
  return [
    ...current,
    ...additions.filter((entry) => !seen.has(entry.id)),
  ].slice(-1_000);
}

function prependBounded(
  current: PagedLogEntry[],
  additions: PagedLogEntry[],
): PagedLogEntry[] {
  const seen = new Set(current.map((entry) => entry.id));
  return [
    ...additions.filter((entry) => !seen.has(entry.id)),
    ...current,
  ].slice(0, 1_000);
}

function messageFor(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
