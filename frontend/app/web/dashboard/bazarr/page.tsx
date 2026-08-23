"use client";

import { FormEvent, useEffect, useState } from "react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type Library = { id: string; name: string };
type Mapping = { libraryId: string; bazarrRootPath: string };
type Settings = {
	configured: boolean;
	address: string;
	port: number;
	baseUrl: string;
	useSsl: boolean;
	apiKeyConfigured: boolean;
	mappings: Mapping[];
	libraries: Library[];
};

const EMPTY: Settings = {
	configured: false,
	address: "localhost",
	port: 6767,
	baseUrl: "",
	useSsl: false,
	apiKeyConfigured: false,
	mappings: [],
	libraries: [],
};

function normalizeSettings(value: unknown): Settings {
	const received =
		value && typeof value === "object" ? (value as Partial<Settings>) : {};

	return {
		...EMPTY,
		...received,
		configured: received.configured === true,
		address:
			typeof received.address === "string" ? received.address : EMPTY.address,
		port:
			typeof received.port === "number" && Number.isFinite(received.port)
				? received.port
				: EMPTY.port,
		baseUrl:
			typeof received.baseUrl === "string" ? received.baseUrl : EMPTY.baseUrl,
		useSsl: received.useSsl === true,
		apiKeyConfigured: received.apiKeyConfigured === true,
		mappings: Array.isArray(received.mappings) ? received.mappings : [],
		libraries: Array.isArray(received.libraries) ? received.libraries : [],
	};
}

export default function BazarrSettingsPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [settings, setSettings] = useState<Settings>(EMPTY);
	const [apiKey, setApiKey] = useState("");
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [message, setMessage] = useState("");

	async function load(current: Session) {
		setLoading(true);
		const response = await adminFetch("/api/admin/bazarr/settings", current);
		const value = await response.json().catch(() => null);
		if (response.ok) {
			setSettings(normalizeSettings(value));
			setMessage("");
		} else {
			setMessage(value?.detail || "Could not load Bazarr settings.");
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

	function updateMapping(libraryId: string, bazarrRootPath: string) {
		setSettings((current) => {
			const mappings = current.mappings.filter(
				(mapping) => mapping.libraryId !== libraryId,
			);
			if (bazarrRootPath.trim()) mappings.push({ libraryId, bazarrRootPath });
			return { ...current, mappings };
		});
	}

	function mappingFor(libraryId: string) {
		return (
			settings.mappings.find((mapping) => mapping.libraryId === libraryId)
				?.bazarrRootPath || ""
		);
	}

	async function save(event: FormEvent) {
		event.preventDefault();
		if (!session) return;
		setSaving(true);
		setMessage("");
		const response = await adminFetch("/api/admin/bazarr/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				address: settings.address,
				port: settings.port,
				baseUrl: settings.baseUrl,
				useSsl: settings.useSsl,
				...(apiKey ? { apiKey } : {}),
				mappings: settings.mappings,
			}),
		});
		const value = await response.json().catch(() => null);
		if (response.ok) {
			setSettings(normalizeSettings(value));
			setApiKey("");
			setMessage("Bazarr settings saved.");
		} else {
			setMessage(value?.detail || "Could not save Bazarr settings.");
		}
		setSaving(false);
	}

	async function remove() {
		if (!session || !window.confirm("Remove the Bazarr configuration?")) return;
		setSaving(true);
		const response = await adminFetch("/api/admin/bazarr/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ enabled: false }),
		});
		const value = await response.json().catch(() => null);
		if (response.ok) {
			setSettings(normalizeSettings(value));
			setApiKey("");
			setMessage("Bazarr configuration removed.");
		} else {
			setMessage(value?.detail || "Could not remove Bazarr settings.");
		}
		setSaving(false);
	}

	return (
		<div className="max-w-4xl">
			<PageHeader
				title="Bazarr"
				description="Find subtitles for the exact episode file selected in ZenStream. Each TV library maps to its Bazarr-visible root path."
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			{loading ? (
				<SurfaceCard className="mt-7 p-6 console-muted">
					Loading Bazarr settings…
				</SurfaceCard>
			) : (
				<form onSubmit={save} className="mt-7 space-y-5">
					<SurfaceCard className="space-y-5 p-6">
						<div className="grid gap-4 sm:grid-cols-[1fr_120px]">
							<label className="block">
								<span className="text-sm font-semibold">Address</span>
								<input
									value={settings.address}
									onChange={(event) =>
										setSettings({ ...settings, address: event.target.value })
									}
									placeholder="localhost"
									className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none"
								/>
							</label>
							<label className="block">
								<span className="text-sm font-semibold">Port</span>
								<input
									type="number"
									min={1}
									max={65535}
									value={settings.port}
									onChange={(event) =>
										setSettings({ ...settings, port: Number(event.target.value) })
									}
									className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none"
								/>
							</label>
						</div>
						<label className="block">
							<span className="text-sm font-semibold">Base URL</span>
							<input
								value={settings.baseUrl}
								onChange={(event) =>
									setSettings({ ...settings, baseUrl: event.target.value })
								}
								placeholder="/"
								className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none"
							/>
						</label>
						<label className="flex items-center gap-3 text-sm">
							<input
								type="checkbox"
								checked={settings.useSsl}
								onChange={(event) =>
									setSettings({ ...settings, useSsl: event.target.checked })
								}
							/>
							Use SSL / HTTPS
						</label>
						<label className="block">
							<span className="text-sm font-semibold">API key</span>
							<input
								type="password"
								value={apiKey}
								onChange={(event) => setApiKey(event.target.value)}
								placeholder={
									settings.apiKeyConfigured
										? "Leave blank to keep the current key"
										: "Paste the Bazarr API key"
								}
								autoComplete="new-password"
								className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none"
							/>
						</label>
					</SurfaceCard>
					<SurfaceCard className="space-y-4 p-6">
						<div>
							<h2 className="text-lg font-bold">Library path mappings</h2>
							<p className="mt-1 text-xs console-muted">
								Enter the root path as Bazarr sees it. ZenStream appends each indexed
								file’s relative path; provider IDs are never used to choose between
								duplicate libraries.
							</p>
						</div>
						{settings.libraries.length === 0 ? (
							<p className="text-sm console-muted">
								Create a TV library before configuring Bazarr.
							</p>
						) : (
							settings.libraries.map((library) => (
								<label key={library.id} className="block">
									<span className="text-sm font-semibold">{library.name}</span>
									<input
										value={mappingFor(library.id)}
										onChange={(event) => updateMapping(library.id, event.target.value)}
										placeholder="/tv"
										className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none"
									/>
								</label>
							))
						)}
					</SurfaceCard>
					<div className="flex items-center justify-between gap-3">
						{settings.configured && (
							<button
								type="button"
								onClick={() => void remove()}
								disabled={saving}
								className="rounded-xl px-4 py-2 text-sm font-semibold text-red-300 hover:bg-red-400/10 disabled:opacity-50"
							>
								Remove configuration
							</button>
						)}
						<button
							type="submit"
							disabled={saving}
							className="console-button-primary ml-auto rounded-xl px-4 py-2 text-sm font-semibold"
						>
							{saving ? "Saving…" : "Save settings"}
						</button>
					</div>
				</form>
			)}
		</div>
	);
}
