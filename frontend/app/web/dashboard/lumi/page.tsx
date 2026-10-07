"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { IconDownload, IconRefresh, IconTrash } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type IntegrationState = "disabled" | "installing" | "ready" | "error";

type InstallProgress = {
	stage: string;
	current: number;
	total: number;
};

type Integration = {
	enabled: boolean;
	installed: boolean;
	state: IntegrationState;
	releaseTag: string | null;
	restartRequired: boolean;
	error: string | null;
	progress: InstallProgress | null;
};

type LumiModel = {
	id: string;
	label: string;
	sizeBytes: number | null;
	installed: boolean;
	enabled: boolean;
	downloading: boolean;
	downloadProgress: number | null;
	downloadStage: string | null;
	downloadError: string | null;
	supportsThinking: boolean;
	isDefault: boolean;
};

type RuntimeLimits = {
	idleUnloadSeconds: number;
	maxContextTokens: number;
	maxOutputTokens: number;
	maxConcurrentChats: number;
	maxActiveConversations: number;
};

type LumiStatus = {
	integration: Integration;
	models: LumiModel[];
	defaultModel: string | null;
	defaultThinking: boolean;
};

type Release = { tag: string; releasedAt: string | null };

type ModelCatalog = {
	models: LumiModel[];
	defaultModel: string | null;
	defaultThinking: boolean;
	limits: RuntimeLimits;
	modelInstallAvailable: boolean;
};

type ModelAction = {
	id: string;
	kind: "enable" | "default" | "download" | "delete";
} | null;

const DEFAULT_RUNTIME_LIMITS: RuntimeLimits = {
	idleUnloadSeconds: 300,
	maxContextTokens: 8192,
	maxOutputTokens: 2048,
	maxConcurrentChats: 1,
	maxActiveConversations: 128,
};

const RUNTIME_LIMIT_FIELDS: {
	key: keyof RuntimeLimits;
	label: string;
	minimum: number;
	maximum: number;
	help: string;
}[] = [
	{
		key: "idleUnloadSeconds",
		label: "Unload model after idle",
		minimum: 0,
		maximum: 86400,
		help: "Seconds; set 0 to keep the model loaded.",
	},
	{
		key: "maxContextTokens",
		label: "Maximum context tokens",
		minimum: 512,
		maximum: 32768,
		help: "The conversation context budget per request.",
	},
	{
		key: "maxOutputTokens",
		label: "Maximum output tokens",
		minimum: 64,
		maximum: 8192,
		help: "Must leave at least 256 tokens inside the context limit.",
	},
	{
		key: "maxConcurrentChats",
		label: "Concurrent chats",
		minimum: 1,
		maximum: 8,
		help: "Maximum simultaneous inference requests.",
	},
	{
		key: "maxActiveConversations",
		label: "Active conversations",
		minimum: 1,
		maximum: 10000,
		help: "Maximum saved conversation records.",
	},
];

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeProgress(value: unknown): InstallProgress | null {
	if (
		!isRecord(value) ||
		typeof value.stage !== "string" ||
		typeof value.current !== "number" ||
		!Number.isFinite(value.current) ||
		typeof value.total !== "number" ||
		!Number.isFinite(value.total)
	) {
		return null;
	}
	return {
		stage: safeText(value.stage),
		current: Math.max(0, value.current),
		total: Math.max(0, value.total),
	};
}

function normalizeIntegration(value: unknown): Integration | null {
	if (
		!isRecord(value) ||
		typeof value.enabled !== "boolean" ||
		typeof value.installed !== "boolean" ||
		!(["disabled", "installing", "ready", "error"] as unknown[]).includes(
			value.state,
		) ||
		!(typeof value.releaseTag === "string" || value.releaseTag === null) ||
		typeof value.restartRequired !== "boolean" ||
		!(typeof value.error === "string" || value.error === null)
	) {
		return null;
	}
	return {
		enabled: value.enabled,
		installed: value.installed,
		state: value.state as IntegrationState,
		releaseTag: value.releaseTag ? safeText(value.releaseTag) : null,
		restartRequired: value.restartRequired,
		error: value.error ? safeText(value.error) : null,
		progress: normalizeProgress(value.progress),
	};
}

function normalizeModel(value: unknown): LumiModel | null {
	if (
		!isRecord(value) ||
		typeof value.id !== "string" ||
		typeof value.label !== "string"
	)
		return null;
	const sizeBytes = value.sizeBytes;
	const downloadProgress = value.downloadProgress;
	const downloadStage = value.downloadStage;
	const downloadError = value.downloadError;
	return {
		id: safeText(value.id),
		label: safeText(value.label),
		sizeBytes:
			typeof sizeBytes === "number" && Number.isFinite(sizeBytes) && sizeBytes >= 0
				? sizeBytes
				: null,
		installed: value.installed === true,
		enabled: value.enabled === true,
		downloading: value.downloading === true,
		downloadProgress:
			typeof downloadProgress === "number" && Number.isFinite(downloadProgress)
				? downloadProgress
				: null,
		downloadStage:
			typeof downloadStage === "string" ? safeText(downloadStage) : null,
		downloadError:
			typeof downloadError === "string" && downloadError.trim()
				? safeText(downloadError.trim())
				: null,
		supportsThinking: value.supportsThinking === true,
		isDefault: value.isDefault === true,
	};
}

function normalizeRuntimeLimits(value: unknown): RuntimeLimits | null {
	if (!isRecord(value)) return null;
	const keys = Object.keys(DEFAULT_RUNTIME_LIMITS) as (keyof RuntimeLimits)[];
	const limits: Partial<RuntimeLimits> = {};
	for (const key of keys) {
		const candidate = value[key];
		if (
			typeof candidate !== "number" ||
			!Number.isSafeInteger(candidate) ||
			candidate < 0
		) {
			return null;
		}
		limits[key] = candidate;
	}
	return limits as RuntimeLimits;
}

function normalizeModels(value: unknown): LumiModel[] | null {
	if (!Array.isArray(value)) return null;
	const result = value.map(normalizeModel);
	return result.every((model): model is LumiModel => model !== null)
		? result
		: null;
}

function normalizeStatus(value: unknown): LumiStatus | null {
	if (!isRecord(value)) return null;
	const integration = normalizeIntegration(value.integration);
	const models = normalizeModels(value.models);
	if (
		!integration ||
		!models ||
		!(typeof value.defaultModel === "string" || value.defaultModel === null) ||
		typeof value.defaultThinking !== "boolean"
	) {
		return null;
	}
	return {
		integration,
		models,
		defaultModel: value.defaultModel ? safeText(value.defaultModel) : null,
		defaultThinking: value.defaultThinking,
	};
}

function normalizeModelCatalog(value: unknown): ModelCatalog | null {
	if (!isRecord(value)) return null;
	const models = normalizeModels(value.models);
	const limits = normalizeRuntimeLimits(value.limits);
	if (
		!models ||
		!limits ||
		!(typeof value.defaultModel === "string" || value.defaultModel === null) ||
		typeof value.defaultThinking !== "boolean" ||
		typeof value.modelInstallAvailable !== "boolean"
	) {
		return null;
	}
	return {
		models,
		defaultModel: value.defaultModel ? safeText(value.defaultModel) : null,
		defaultThinking: value.defaultThinking,
		limits,
		modelInstallAvailable: value.modelInstallAvailable,
	};
}

function normalizeReleases(value: unknown): Release[] | null {
	if (!isRecord(value) || !Array.isArray(value.releases)) return null;
	const releases: Release[] = [];
	for (const entry of value.releases) {
		if (
			!isRecord(entry) ||
			typeof entry.tag !== "string" ||
			!(typeof entry.releasedAt === "string" || entry.releasedAt === null)
		) {
			return null;
		}
		releases.push({
			tag: safeText(entry.tag),
			releasedAt: entry.releasedAt,
		});
	}
	return releases;
}

function safeText(value: string) {
	return value.replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 500);
}

function responseError(value: unknown, fallback: string) {
	if (isRecord(value) && typeof value.detail === "string" && value.detail.trim())
		return safeText(value.detail.trim());
	return fallback;
}

function formatBytes(value: number | null) {
	if (value === null) return "Size unavailable";
	if (value === 0) return "0 B";
	const units = ["B", "KiB", "MiB", "GiB", "TiB"];
	const exponent = Math.max(
		0,
		Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1),
	);
	return `${(value / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

function progressPercent(value: number | null) {
	if (value === null) return null;
	const percent = value >= 0 && value <= 1 ? value * 100 : value;
	return Math.round(Math.min(100, Math.max(0, percent)));
}

function formatReleaseDate(value: string | null) {
	if (!value) return "Release date unavailable";
	const date = new Date(value);
	return Number.isNaN(date.getTime())
		? "Release date unavailable"
		: date.toLocaleDateString();
}

function mergeModels(
	statusModels: LumiModel[],
	catalogModels: LumiModel[],
	defaultModel: string | null,
) {
	const statusById = new Map(statusModels.map((model) => [model.id, model]));
	const sourceModels = catalogModels.length > 0 ? catalogModels : statusModels;
	return sourceModels.map((model) => {
		const status = statusById.get(model.id);
		return {
			...status,
			...model,
			installed: status?.installed ?? model.installed,
			downloading: status?.downloading ?? model.downloading,
			downloadProgress: status?.downloadProgress ?? model.downloadProgress,
			isDefault: model.id === defaultModel,
		};
	});
}

export default function LumiSettingsPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [integration, setIntegration] = useState<Integration | null>(null);
	const [models, setModels] = useState<LumiModel[]>([]);
	const [releases, setReleases] = useState<Release[]>([]);
	const [defaultModel, setDefaultModel] = useState<string | null>(null);
	const [defaultThinking, setDefaultThinking] = useState(false);
	const [runtimeLimits, setRuntimeLimits] = useState(DEFAULT_RUNTIME_LIMITS);
	const [runtimeSettingsDirty, setRuntimeSettingsDirty] = useState(false);
	const [runtimeSettingsBusy, setRuntimeSettingsBusy] = useState(false);
	const [modelInstallAvailable, setModelInstallAvailable] = useState(false);
	const [selectedReleaseTag, setSelectedReleaseTag] = useState("");
	const [loading, setLoading] = useState(true);
	const [integrationBusy, setIntegrationBusy] = useState(false);
	const [modelAction, setModelAction] = useState<ModelAction>(null);
	const [error, setError] = useState("");
	const [releaseError, setReleaseError] = useState("");
	const [modelsError, setModelsError] = useState("");
	const [message, setMessage] = useState("");
	const requestInFlight = useRef(false);
	const runtimeSettingsDirtyRef = useRef(false);

	const load = useCallback(async (current: Session, silent = false) => {
		if (requestInFlight.current) return;
		requestInFlight.current = true;
		if (!silent) {
			setLoading(true);
			setError("");
		}
		try {
			const [statusResponse, releasesResponse, modelsResponse] = await Promise.all(
				[
					adminFetch("/api/admin/lumi/status", current),
					adminFetch("/api/admin/lumi/releases", current),
					adminFetch("/api/admin/lumi/models", current),
				],
			);
			const [statusValue, releasesValue, modelsValue] = await Promise.all([
				statusResponse.json().catch(() => null),
				releasesResponse.json().catch(() => null),
				modelsResponse.json().catch(() => null),
			]);
			if (!statusResponse.ok) {
				throw new Error(
					responseError(statusValue, "Could not load Lumi integration status."),
				);
			}
			const status = normalizeStatus(statusValue);
			if (!status) throw new Error("The Lumi status response was not recognized.");

			const modelCatalog = modelsResponse.ok
				? normalizeModelCatalog(modelsValue)
				: null;
			const nextDefaultModel = modelCatalog
				? modelCatalog.defaultModel
				: status.defaultModel;
			setIntegration(status.integration);
			setModels(
				mergeModels(status.models, modelCatalog?.models ?? [], nextDefaultModel),
			);
			setDefaultModel(nextDefaultModel);
			if (!runtimeSettingsDirtyRef.current) {
				setDefaultThinking(modelCatalog?.defaultThinking ?? status.defaultThinking);
				if (modelCatalog) setRuntimeLimits(modelCatalog.limits);
			}
			setModelInstallAvailable(modelCatalog?.modelInstallAvailable ?? false);
			setSelectedReleaseTag(
				(currentTag) =>
					currentTag ||
					(status.integration.enabled ? status.integration.releaseTag || "" : ""),
			);
			setError("");
			if (!releasesResponse.ok) {
				setReleaseError(
					responseError(releasesValue, "Could not load supported Lumi releases."),
				);
			} else {
				const normalizedReleases = normalizeReleases(releasesValue);
				if (normalizedReleases) {
					setReleases(normalizedReleases);
					setReleaseError("");
				} else {
					setReleaseError("The Lumi release list response was not recognized.");
				}
			}
			if (!modelsResponse.ok) {
				setModelsError(
					responseError(modelsValue, "Could not load Lumi model settings."),
				);
			} else if (!modelCatalog) {
				setModelsError("The Lumi model list response was not recognized.");
			} else {
				setModelsError("");
			}
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			requestInFlight.current = false;
			if (!silent) setLoading(false);
		}
	}, []);

	useEffect(() => {
		const current = readSession();
		if (current) {
			setSession(current);
			void load(current);
		} else {
			setLoading(false);
		}
	}, [load]);

	const hasDownload = models.some((model) => model.downloading);
	const shouldPoll =
		integrationBusy || integration?.state === "installing" || hasDownload;

	useEffect(() => {
		if (!session || !shouldPoll) return;
		const timer = window.setInterval(() => void load(session, true), 1500);
		return () => window.clearInterval(timer);
	}, [load, session, shouldPoll]);

	async function updateIntegration(enabled: boolean) {
		if (!session) return;
		if (enabled && !selectedReleaseTag) {
			setError("Select a supported Lumi release before enabling the integration.");
			return;
		}
		setIntegrationBusy(true);
		setError("");
		setMessage("");
		try {
			const response = await adminFetch("/api/admin/lumi/settings", session, {
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					enabled,
					...(enabled ? { releaseTag: selectedReleaseTag } : {}),
				}),
			});
			const value = await response.json().catch(() => null);
			if (!response.ok) {
				throw new Error(
					responseError(value, `Could not ${enabled ? "enable" : "disable"} Lumi.`),
				);
			}
			setMessage(
				enabled
					? "Lumi installation and enable request submitted."
					: "Lumi disabled.",
			);
			if (!enabled) setSelectedReleaseTag("");
			await load(session, true);
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			setIntegrationBusy(false);
		}
	}

	async function updateModel(model: LumiModel, enabled: boolean) {
		if (!session) return;
		if (enabled && !model.installed) {
			setError("Download this model before enabling it.");
			return;
		}
		if (!enabled && model.isDefault) {
			setError("Choose another default model before disabling this one.");
			return;
		}
		setModelAction({ id: model.id, kind: "enable" });
		setError("");
		setMessage("");
		try {
			const response = await adminFetch(
				`/api/admin/lumi/models/${encodeURIComponent(model.id)}`,
				session,
				{
					method: "PATCH",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ enabled }),
				},
			);
			const value = await response.json().catch(() => null);
			if (!response.ok)
				throw new Error(responseError(value, "Could not update this model."));
			setMessage(`${model.label} ${enabled ? "enabled" : "disabled"}.`);
			await load(session, true);
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			setModelAction(null);
		}
	}

	async function makeDefault(model: LumiModel) {
		if (!session || !model.installed || model.downloading) return;
		setModelAction({ id: model.id, kind: "default" });
		setError("");
		setMessage("");
		try {
			const response = await adminFetch(
				`/api/admin/lumi/models/${encodeURIComponent(model.id)}`,
				session,
				{
					method: "PATCH",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ enabled: true, isDefault: true }),
				},
			);
			const value = await response.json().catch(() => null);
			if (!response.ok)
				throw new Error(responseError(value, "Could not set the default model."));
			setMessage(`${model.label} is now the default model.`);
			await load(session, true);
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			setModelAction(null);
		}
	}

	async function downloadModel(model: LumiModel) {
		if (
			!session ||
			!integration?.enabled ||
			!modelInstallAvailable ||
			model.installed ||
			model.downloading
		)
			return;
		setModelAction({ id: model.id, kind: "download" });
		setError("");
		setMessage("");
		try {
			const response = await adminFetch(
				`/api/admin/lumi/models/${encodeURIComponent(model.id)}/download`,
				session,
				{ method: "POST" },
			);
			const value = await response.json().catch(() => null);
			if (!response.ok)
				throw new Error(
					responseError(value, "Could not start this model download."),
				);
			setMessage(`${model.label} model download started.`);
			await load(session, true);
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			setModelAction(null);
		}
	}

	function changeRuntimeLimit(key: keyof RuntimeLimits, rawValue: string) {
		runtimeSettingsDirtyRef.current = true;
		setRuntimeSettingsDirty(true);
		const value = Number(rawValue);
		if (!Number.isFinite(value)) return;
		setRuntimeLimits((current) => ({
			...current,
			[key]: Math.trunc(value),
		}));
	}

	async function saveRuntimeSettings() {
		if (!session || !runtimeSettingsDirty) return;
		setRuntimeSettingsBusy(true);
		setError("");
		setMessage("");
		try {
			const response = await adminFetch(
				"/api/admin/lumi/models/settings",
				session,
				{
					method: "PATCH",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						defaultThinking,
						limits: runtimeLimits,
					}),
				},
			);
			const value = await response.json().catch(() => null);
			if (!response.ok)
				throw new Error(
					responseError(value, "Could not save Lumi runtime settings."),
				);
			runtimeSettingsDirtyRef.current = false;
			setRuntimeSettingsDirty(false);
			setMessage("Lumi runtime settings saved.");
			await load(session, true);
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			setRuntimeSettingsBusy(false);
		}
	}

	async function deleteModel(model: LumiModel) {
		if (!session || !model.installed || model.enabled || model.isDefault) return;
		if (
			!window.confirm(
				`Delete the downloaded files for ${model.label}? You can download it again later.`,
			)
		)
			return;
		setModelAction({ id: model.id, kind: "delete" });
		setError("");
		setMessage("");
		try {
			const response = await adminFetch(
				`/api/admin/lumi/models/${encodeURIComponent(model.id)}`,
				session,
				{ method: "DELETE" },
			);
			const value = await response.json().catch(() => null);
			if (!response.ok)
				throw new Error(responseError(value, "Could not delete this model."));
			setMessage(`${model.label} model files deleted.`);
			await load(session, true);
		} catch (cause) {
			setError(
				cause instanceof Error && cause.message
					? safeText(cause.message)
					: "Could not connect to the Orchestrator.",
			);
		} finally {
			setModelAction(null);
		}
	}

	const refreshing = loading || integrationBusy || modelAction !== null;
	const canEnable = Boolean(
		session &&
		integration &&
		integration.state !== "installing" &&
		!integration.enabled &&
		selectedReleaseTag &&
		!integrationBusy,
	);

	return (
		<div className="max-w-5xl">
			<PageHeader
				title="Lumi assistant"
				description="Install a supported Lumi release and manage its local models. Lumi runs inside the Orchestrator when enabled."
				actions={
					<button
						type="button"
						onClick={() => (session ? void load(session) : undefined)}
						disabled={!session || refreshing}
						className="console-button inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-semibold disabled:opacity-40"
					>
						<IconRefresh size={16} />
						Refresh
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			{error && (
				<p
					role="alert"
					className="mt-5 rounded-lg border border-red-900/70 bg-red-950/30 px-4 py-3 text-sm text-red-200"
				>
					{error}
				</p>
			)}
			{loading ? (
				<SurfaceCard className="mt-7 p-6 console-muted">
					Loading Lumi integration status…
				</SurfaceCard>
			) : !session ? (
				<SurfaceCard className="mt-7 p-6 console-muted">
					Sign in to manage the Lumi integration.
				</SurfaceCard>
			) : (
				<div className="mt-7 space-y-5">
					<SurfaceCard className="space-y-5 p-6">
						<div className="flex flex-wrap items-start justify-between gap-4">
							<div>
								<h2 className="text-lg font-bold">Integration</h2>
								<p className="mt-2 text-sm leading-6 console-muted">
									Lumi is installed from a supported release only after you select a
									version and enable it. The selected package is loaded in-process.
								</p>
							</div>
							{integration && (
								<div className="flex flex-wrap gap-2 text-xs font-semibold">
									<span
										className={`rounded-full px-3 py-1 ${integration.enabled ? "bg-emerald-950/60 text-emerald-300" : "bg-white/5 text-white/55"}`}
									>
										{integration.enabled ? "Enabled" : "Disabled"}
									</span>
									<span className="rounded-full bg-white/5 px-3 py-1 capitalize text-white/55">
										{integration.state}
									</span>
									<span
										className={`rounded-full px-3 py-1 ${integration.installed ? "bg-cyan-950/50 text-cyan-200" : "bg-white/5 text-white/55"}`}
									>
										{integration.installed
											? "Package installed"
											: "Package not installed"}
									</span>
								</div>
							)}
						</div>

						{integration?.releaseTag && (
							<p className="text-sm text-white/70">
								Selected release:{" "}
								<span className="font-semibold">{integration.releaseTag}</span>
							</p>
						)}

						{integration?.state === "installing" && (
							<ProgressPanel
								title="Installing Lumi"
								stage={integration.progress?.stage || "Preparing release"}
								current={integration.progress?.current ?? null}
								total={integration.progress?.total ?? null}
							/>
						)}

						{integration?.state === "error" && integration.error && (
							<p
								role="alert"
								className="rounded-lg border border-red-900/60 bg-red-950/25 px-4 py-3 text-sm text-red-200"
							>
								{safeText(integration.error)}
							</p>
						)}

						<div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
							<label className="block min-w-0">
								<span className="text-sm font-semibold">Supported release</span>
								<select
									value={selectedReleaseTag}
									onChange={(event) => setSelectedReleaseTag(event.target.value)}
									disabled={
										integration?.enabled ||
										integration?.state === "installing" ||
										integrationBusy ||
										releases.length === 0
									}
									className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none disabled:opacity-40"
								>
									<option value="">
										{releases.length
											? "Select a release"
											: "No supported releases available"}
									</option>
									{integration?.releaseTag &&
										!releases.some(
											(release) => release.tag === integration.releaseTag,
										) && (
											<option value={integration.releaseTag}>
												{integration.releaseTag} (currently installed)
											</option>
										)}
									{releases.map((release) => (
										<option key={release.tag} value={release.tag}>
											{release.tag} · {formatReleaseDate(release.releasedAt)}
										</option>
									))}
								</select>
								{releaseError ? (
									<span className="mt-2 block text-xs text-amber-200" role="alert">
										{releaseError}
									</span>
								) : releases.length === 0 ? (
									<span className="mt-2 block text-xs console-muted">
										No supported Lumi releases are currently available.
									</span>
								) : (
									<span className="mt-2 block text-xs console-muted">
										Choose a published stable release to install or enable.
									</span>
								)}
							</label>
							<div className="flex flex-wrap gap-2">
								{integration?.enabled ? (
									<button
										type="button"
										onClick={() => void updateIntegration(false)}
										disabled={
											integrationBusy || integration.state === "installing" || hasDownload
										}
										className="console-button rounded-xl px-4 py-2.5 text-sm font-semibold disabled:opacity-40"
									>
										{integrationBusy ? "Disabling…" : "Disable Lumi"}
									</button>
								) : (
									<button
										type="button"
										onClick={() => void updateIntegration(true)}
										disabled={!canEnable || loading}
										className="console-button-primary rounded-xl px-4 py-2.5 text-sm font-semibold disabled:opacity-40"
									>
										{integrationBusy
											? "Installing…"
											: integration?.state === "error"
												? "Retry installation"
												: integration?.installed
													? "Enable Lumi"
													: "Install and enable Lumi"}
									</button>
								)}
							</div>
						</div>
					</SurfaceCard>

					<SurfaceCard className="space-y-5 p-6">
						<div className="flex flex-wrap items-start justify-between gap-3">
							<div>
								<h2 className="text-lg font-bold">Model files and access</h2>
								<p className="mt-2 text-sm leading-6 console-muted">
									Enable downloaded models for chat users, set the default, or remove
									local model files. Download size is shown for each model.
								</p>
							</div>
							{defaultModel && (
								<p className="text-xs console-muted">
									Default:{" "}
									<span className="font-semibold text-white/75">{defaultModel}</span>
									<span className="mx-1">·</span>
									Thinking {defaultThinking ? "on" : "off"}
								</p>
							)}
						</div>
						{modelsError && (
							<p role="alert" className="text-sm text-amber-200">
								{modelsError}
							</p>
						)}
						{!integration?.installed && (
							<p className="rounded-lg border border-white/10 bg-white/[0.025] px-4 py-3 text-sm console-muted">
								Install a Lumi release before managing model files.
							</p>
						)}
						{integration?.installed && (
							<p className="rounded-lg border border-white/10 bg-white/[0.025] px-4 py-3 text-sm console-muted">
								{!integration.enabled
									? "Enable the Lumi integration before managing model files."
									: !modelInstallAvailable
										? "This release has no model installer assets. Install a supported release that includes them to download Qwen3.5 models."
										: "Model files are installed locally by Lumi. The Orchestrator does not use a model API URL."}
							</p>
						)}
						{models.length === 0 ? (
							<p className="text-sm console-muted">
								No models are currently available.
							</p>
						) : (
							<ul className="divide-y divide-white/5">
								{models.map((model) => {
									const busy = modelAction?.id === model.id;
									const progress = progressPercent(model.downloadProgress);
									const mayDisable = model.enabled && !model.isDefault;
									return (
										<li
											key={model.id}
											className="flex flex-col gap-4 py-5 first:pt-0 last:pb-0 xl:flex-row xl:items-center xl:justify-between"
										>
											<div className="min-w-0 flex-1">
												<div className="flex flex-wrap items-center gap-2">
													<h3 className="text-sm font-semibold text-white">{model.label}</h3>
													{model.isDefault && (
														<span className="rounded-full bg-violet-950/70 px-2.5 py-1 text-[11px] font-semibold text-violet-200">
															Default
														</span>
													)}
													<span
														className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${model.installed ? "bg-emerald-950/60 text-emerald-200" : "bg-white/5 text-white/50"}`}
													>
														{model.installed ? "Installed" : "Not installed"}
													</span>
													{model.enabled && (
														<span className="rounded-full bg-cyan-950/60 px-2.5 py-1 text-[11px] font-semibold text-cyan-200">
															Enabled for chat
														</span>
													)}
												</div>
												<p className="mt-1 break-all text-xs console-muted">{model.id}</p>
												<p className="mt-1 text-xs console-muted">
													Download size: {formatBytes(model.sizeBytes)}
													{model.supportsThinking && " · Supports thinking"}
												</p>
												{model.downloading && (
													<div className="mt-3 max-w-xl">
														<div className="mb-1 flex justify-between text-xs console-muted">
															<span>{model.downloadStage || "Downloading model"}</span>
															<span>{progress === null ? "In progress" : `${progress}%`}</span>
														</div>
														<div
															role="progressbar"
															aria-label={`${model.label} download progress`}
															aria-valuemin={0}
															aria-valuemax={100}
															aria-valuenow={progress ?? undefined}
															className="h-1.5 overflow-hidden rounded-full bg-white/10"
														>
															<div
																className={`h-full rounded-full bg-cyan-400 ${progress === null ? "w-1/3 animate-pulse" : ""}`}
																style={
																	progress === null ? undefined : { width: `${progress}%` }
																}
															/>
														</div>
													</div>
												)}
												{model.downloadError && !model.downloading && (
													<p role="alert" className="mt-3 max-w-xl text-xs text-red-200">
														{safeText(model.downloadError)}
													</p>
												)}
											</div>
											<div className="flex flex-wrap items-center gap-3 xl:justify-end">
												{model.installed && (
													<label className="flex items-center gap-2 text-xs text-white/75">
														<input
															type="checkbox"
															checked={model.enabled}
															disabled={
																!integration?.enabled ||
																busy ||
																modelAction !== null ||
																(!model.enabled && model.downloading) ||
																(model.enabled && model.isDefault)
															}
															onChange={(event) =>
																void updateModel(model, event.target.checked)
															}
															className="h-4 w-4 accent-cyan-400"
														/>
														<span>Enabled</span>
													</label>
												)}
												<button
													type="button"
													onClick={() => void makeDefault(model)}
													disabled={
														!integration?.enabled ||
														!model.installed ||
														model.isDefault ||
														model.downloading ||
														modelAction !== null
													}
													className="console-button rounded-xl px-3 py-2 text-xs font-semibold disabled:opacity-40"
												>
													{busy && modelAction?.kind === "default"
														? "Saving…"
														: "Make default"}
												</button>
												{model.downloading ? (
													<span className="inline-flex items-center gap-2 text-xs text-cyan-200">
														<IconDownload size={15} /> Downloading…
													</span>
												) : !model.installed ? (
													modelInstallAvailable ? (
														<button
															type="button"
															onClick={() => void downloadModel(model)}
															disabled={
																!integration?.enabled ||
																modelAction !== null ||
																!modelInstallAvailable
															}
															className="console-button inline-flex items-center gap-2 rounded-xl px-3 py-2 text-xs font-semibold disabled:opacity-40"
														>
															<IconDownload size={15} />
															{busy && modelAction?.kind === "download"
																? "Starting…"
																: "Download model"}
														</button>
													) : (
														<span
															className="text-xs console-muted"
															title="This release does not include installer assets"
														>
															Installer unavailable
														</span>
													)
												) : (
													<button
														type="button"
														onClick={() => void deleteModel(model)}
														disabled={
															!integration?.enabled ||
															model.enabled ||
															model.isDefault ||
															model.downloading ||
															modelAction !== null
														}
														className="console-button inline-flex items-center gap-2 rounded-xl px-3 py-2 text-xs font-semibold text-red-200 disabled:opacity-40"
													>
														<IconTrash size={15} />
														{busy && modelAction?.kind === "delete"
															? "Deleting…"
															: "Delete files"}
													</button>
												)}
												{model.enabled && model.isDefault && (
													<span className="text-[11px] console-muted">
														Choose another default before disabling or deleting.
													</span>
												)}
											</div>
										</li>
									);
								})}
							</ul>
						)}
					</SurfaceCard>

					<SurfaceCard className="space-y-5 p-6">
						<div>
							<h2 className="text-lg font-bold">
								Default behavior and runtime limits
							</h2>
							<p className="mt-2 text-sm leading-6 console-muted">
								These server-wide settings apply to new conversations and bound
								Lumi&apos;s local inference workload.
							</p>
						</div>
						<label className="flex items-start gap-3 rounded-xl border border-white/10 bg-white/[0.025] p-4">
							<input
								type="checkbox"
								checked={defaultThinking}
								disabled={runtimeSettingsBusy}
								onChange={(event) => {
									runtimeSettingsDirtyRef.current = true;
									setRuntimeSettingsDirty(true);
									setDefaultThinking(event.target.checked);
								}}
								className="mt-0.5 h-4 w-4 accent-cyan-400"
							/>
							<span>
								<span className="block text-sm font-semibold text-white">
									Enable thinking by default
								</span>
								<span className="mt-1 block text-xs console-muted">
									Users can still change thinking for each conversation when the selected
									model supports it.
								</span>
							</span>
						</label>
						<div className="grid gap-4 sm:grid-cols-2">
							{RUNTIME_LIMIT_FIELDS.map((field) => (
								<label key={field.key} className="block">
									<span className="text-sm font-semibold">{field.label}</span>
									<input
										type="number"
										min={field.minimum}
										max={field.maximum}
										step={1}
										value={runtimeLimits[field.key]}
										disabled={runtimeSettingsBusy}
										onChange={(event) =>
											changeRuntimeLimit(field.key, event.target.value)
										}
										className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none disabled:opacity-40"
									/>
									<span className="mt-1 block text-xs console-muted">
										{field.help} Range: {field.minimum.toLocaleString()}–
										{field.maximum.toLocaleString()}.
									</span>
								</label>
							))}
						</div>
						<div className="flex flex-wrap items-center justify-between gap-3">
							<p className="text-xs console-muted">
								Changes are saved to this Orchestrator and do not call an external model
								API.
							</p>
							<button
								type="button"
								onClick={() => void saveRuntimeSettings()}
								disabled={
									!runtimeSettingsDirty || runtimeSettingsBusy || loading || !session
								}
								className="console-button-primary rounded-xl px-4 py-2.5 text-sm font-semibold disabled:opacity-40"
							>
								{runtimeSettingsBusy ? "Saving…" : "Save runtime settings"}
							</button>
						</div>
					</SurfaceCard>
				</div>
			)}
		</div>
	);
}

function ProgressPanel({
	title,
	stage,
	current,
	total,
}: {
	title: string;
	stage: string;
	current: number | null;
	total: number | null;
}) {
	const percent =
		current !== null && total !== null && total > 0
			? Math.round(Math.min(100, Math.max(0, (current / total) * 100)))
			: null;
	return (
		<div className="rounded-xl border border-cyan-900/60 bg-cyan-950/20 p-4">
			<div className="flex flex-wrap items-center justify-between gap-2 text-sm">
				<span className="font-semibold text-cyan-100">{title}</span>
				<span className="text-xs text-cyan-100/70">
					{percent === null ? "In progress" : `${percent}%`}
				</span>
			</div>
			<p className="mt-1 text-xs text-cyan-100/65">
				{stage}
				{current !== null &&
					total !== null &&
					total > 0 &&
					` · ${current} of ${total}`}
			</p>
			<div
				role="progressbar"
				aria-label={`${title}: ${stage}`}
				aria-valuemin={0}
				aria-valuemax={100}
				aria-valuenow={percent ?? undefined}
				className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/10"
			>
				<div
					className={`h-full rounded-full bg-cyan-400 ${percent === null ? "w-1/3 animate-pulse" : ""}`}
					style={percent === null ? undefined : { width: `${percent}%` }}
				/>
			</div>
		</div>
	);
}
