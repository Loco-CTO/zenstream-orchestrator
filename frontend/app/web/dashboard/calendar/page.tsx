"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import {
	IconCalendar,
	IconDeviceTv,
	IconMovie,
	IconRefresh,
	IconTrash,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type Provider = "sonarr" | "radarr";

type Library = {
	id: string;
	name: string;
	type: "tv_series" | "movies" | string;
};

type ServiceSettings = {
	provider: Provider;
	address: string;
	port: number;
	baseUrl: string;
	useSsl: boolean;
	libraryId: string;
	libraryName?: string;
	apiKeyConfigured: boolean;
	configured: boolean;
	validatedAt?: string | null;
	lastSyncAt?: string | null;
	lastError?: string | null;
	apiKey: string;
};

const DEFAULTS: Record<Provider, ServiceSettings> = {
	sonarr: {
		provider: "sonarr",
		address: "",
		port: 8989,
		baseUrl: "/",
		useSsl: false,
		libraryId: "",
		apiKeyConfigured: false,
		configured: false,
		apiKey: "",
	},
	radarr: {
		provider: "radarr",
		address: "",
		port: 7878,
		baseUrl: "/",
		useSsl: false,
		libraryId: "",
		apiKeyConfigured: false,
		configured: false,
		apiKey: "",
	},
};

function mergeSettings(
	provider: Provider,
	value: Partial<ServiceSettings> | undefined,
): ServiceSettings {
	return {
		...DEFAULTS[provider],
		...(value || {}),
		provider,
		apiKey: "",
	};
}

export default function CalendarSettingsPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [services, setServices] = useState<Record<Provider, ServiceSettings>>({
		sonarr: DEFAULTS.sonarr,
		radarr: DEFAULTS.radarr,
	});
	const [libraries, setLibraries] = useState<Library[]>([]);
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState<Provider | null>(null);
	const [message, setMessage] = useState("");

	async function load(current: Session) {
		setLoading(true);
		const [settingsResponse, librariesResponse] = await Promise.all([
			adminFetch("/api/admin/calendar/settings", current),
			adminFetch("/api/admin/libraries", current),
		]);
		const settings = await settingsResponse.json().catch(() => null);
		const libraryValues = await librariesResponse.json().catch(() => null);
		if (settingsResponse.ok) {
			setServices({
				sonarr: mergeSettings("sonarr", settings?.sonarr),
				radarr: mergeSettings("radarr", settings?.radarr),
			});
		} else {
			setMessage(settings?.detail || "Could not load calendar settings.");
		}
		if (librariesResponse.ok && Array.isArray(libraryValues)) {
			setLibraries(libraryValues);
		} else if (!settingsResponse.ok) {
			setMessage(
				libraryValues?.detail || settings?.detail || "Could not load libraries.",
			);
		}
		setLoading(false);
	}

	useEffect(() => {
		const current = readSession();
		if (current) {
			setSession(current);
			void load(current);
		} else {
			setLoading(false);
		}
	}, []);

	const update = (provider: Provider, values: Partial<ServiceSettings>) => {
		setServices((current) => ({
			...current,
			[provider]: { ...current[provider], ...values },
		}));
	};

	async function save(event: FormEvent, provider: Provider) {
		event.preventDefault();
		if (!session) return;
		const service = services[provider];
		setSaving(provider);
		setMessage("");
		const response = await adminFetch("/api/admin/calendar/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				[provider]: {
					address: service.address,
					port: service.port,
					baseUrl: service.baseUrl,
					useSsl: service.useSsl,
					libraryId: service.libraryId,
					...(service.apiKey ? { apiKey: service.apiKey } : {}),
				},
			}),
		});
		const data = await response.json().catch(() => null);
		if (response.ok) {
			setServices((current) => ({
				...current,
				[provider]: mergeSettings(provider, data?.[provider]),
			}));
			setMessage(`${provider === "sonarr" ? "Sonarr" : "Radarr"} settings saved.`);
		} else {
			setMessage(data?.detail || "Could not save calendar settings.");
		}
		setSaving(null);
	}

	async function remove(provider: Provider) {
		if (!session || !window.confirm(`Remove ${provider === "sonarr" ? "Sonarr" : "Radarr"} calendar configuration?`)) return;
		setSaving(provider);
		const response = await adminFetch("/api/admin/calendar/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ [provider]: { enabled: false } }),
		});
		const data = await response.json().catch(() => null);
		if (response.ok) {
			setServices((current) => ({
				...current,
				[provider]: mergeSettings(provider, data?.[provider]),
			}));
			setMessage(`${provider === "sonarr" ? "Sonarr" : "Radarr"} configuration removed.`);
		} else {
			setMessage(data?.detail || "Could not remove calendar settings.");
		}
		setSaving(null);
	}

	return (
		<div className="max-w-4xl">
			<PageHeader
				title="Calendar"
				description="Connect Sonarr and Radarr to show upcoming releases beside your catalog media."
				actions={
					<button
						onClick={() => session && void load(session)}
						className="material-icon-button"
						aria-label="Refresh calendar settings"
						title="Refresh calendar settings"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			{loading ? (
				<SurfaceCard className="mt-7 p-6 console-muted">Loading calendar settings…</SurfaceCard>
			) : (
				<div className="mt-7 grid gap-5 xl:grid-cols-2">
					<ServiceCard
						provider="sonarr"
						service={services.sonarr}
						libraries={libraries.filter((library) => library.type === "tv_series")}
						saving={saving === "sonarr"}
						onChange={update}
						onSave={save}
						onRemove={remove}
					/>
					<ServiceCard
						provider="radarr"
						service={services.radarr}
						libraries={libraries.filter((library) => library.type === "movies")}
						saving={saving === "radarr"}
						onChange={update}
						onSave={save}
						onRemove={remove}
					/>
				</div>
			)}
		</div>
	);
}

function ServiceCard({
	provider,
	service,
	libraries,
	saving,
	onChange,
	onSave,
	onRemove,
}: {
	provider: Provider;
	service: ServiceSettings;
	libraries: Library[];
	saving: boolean;
	onChange: (provider: Provider, values: Partial<ServiceSettings>) => void;
	onSave: (event: FormEvent, provider: Provider) => void;
	onRemove: (provider: Provider) => void;
}) {
	const isSonarr = provider === "sonarr";
	const Icon = isSonarr ? IconDeviceTv : IconMovie;
	const title = isSonarr ? "Sonarr" : "Radarr";
	const canRemove = service.configured;
	const selectedLibrary = useMemo(
		() => libraries.find((library) => library.id === service.libraryId),
		[libraries, service.libraryId],
	);

	return (
		<SurfaceCard className="p-6">
			<div className="flex items-start justify-between gap-4">
				<div className="flex items-start gap-3">
					<div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#5ee3d8]/10 text-[#5ee3d8]"><Icon size={21} /></div>
					<div>
						<h2 className="text-xl font-bold">{title}</h2>
						<p className="mt-1 text-xs console-muted">{isSonarr ? "Episode air dates" : "Movie release dates"}</p>
					</div>
				</div>
				{service.validatedAt && <span className="rounded-full bg-emerald-400/10 px-2.5 py-1 text-[11px] font-semibold text-emerald-300">Connected</span>}
			</div>
			<form onSubmit={(event) => onSave(event, provider)} className="mt-6 space-y-4">
				<label className="block">
					<span className="text-sm font-semibold">Address</span>
					<input value={service.address} onChange={(event) => onChange(provider, { address: event.target.value })} placeholder="localhost" className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none" />
				</label>
				<div className="grid gap-4 sm:grid-cols-[120px_1fr]">
					<label className="block">
						<span className="text-sm font-semibold">Port</span>
						<input type="number" min={1} max={65535} value={service.port} onChange={(event) => onChange(provider, { port: Number(event.target.value) })} className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none" />
					</label>
					<label className="block">
						<span className="text-sm font-semibold">Base URL</span>
						<input value={service.baseUrl} onChange={(event) => onChange(provider, { baseUrl: event.target.value })} placeholder="/" className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none" />
					</label>
				</div>
				<label className="flex items-center gap-3 text-sm">
					<input type="checkbox" checked={service.useSsl} onChange={(event) => onChange(provider, { useSsl: event.target.checked })} />
					Use SSL / HTTPS
				</label>
				<label className="block">
					<span className="text-sm font-semibold">API key</span>
					<input type="password" value={service.apiKey} onChange={(event) => onChange(provider, { apiKey: event.target.value })} placeholder={service.apiKeyConfigured ? "Leave blank to keep the current key" : "Paste the service API key"} autoComplete="new-password" className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none" />
				</label>
				<label className="block">
					<span className="text-sm font-semibold">ZenStream library</span>
					<select value={service.libraryId} onChange={(event) => onChange(provider, { libraryId: event.target.value })} className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none">
						<option value="">Choose a library</option>
						{libraries.map((library) => <option key={library.id} value={library.id}>{library.name}</option>)}
					</select>
					{selectedLibrary && <span className="mt-2 block text-xs console-muted">Calendar events are visible only to users granted this library.</span>}
				</label>
				<div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/8 pt-5">
					{service.lastError ? <span className="max-w-[70%] text-xs text-amber-200/80">Last sync: {service.lastError}</span> : <span className="text-xs console-muted">Daily sync at 03:00; future metadata refetch at 04:00.</span>}
					<div className="flex items-center gap-2">
						{canRemove && <button type="button" onClick={() => onRemove(provider)} className="material-icon-button text-red-300/80" aria-label={`Remove ${title}`} title={`Remove ${title}`}><IconTrash size={17} /></button>}
						<button type="submit" disabled={saving} className="console-button-primary rounded-xl px-4 py-2 text-sm font-semibold">{saving ? "Saving…" : "Save settings"}</button>
					</div>
				</div>
			</form>
		</SurfaceCard>
	);
}

