"use client";

import { FormEvent, useEffect, useState } from "react";
import {
	IconCheck,
	IconKey,
	IconMusic,
	IconPlus,
	IconRefresh,
	IconX,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import { PageHeader, StatusMessage } from "../components/dashboard-surface";

type ProviderState = {
	configured: boolean;
	credential?: string;
	credentialType?: string;
	validatedAt?: string | null;
};
type ProviderMap = Record<string, ProviderState>;
type LanguageOption = { value: string; label: string };

export default function MetadataPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [providers, setProviders] = useState<ProviderMap>({});
	const [tmdb, setTmdb] = useState("");
	const [tmdbType, setTmdbType] = useState("api_key");
	const [tvdb, setTvdb] = useState("");
	const [pin, setPin] = useState("");
	const [message, setMessage] = useState("");
	const [locales, setLocales] = useState<string[]>(["en"]);
	const [preferNoLanguageForBackdrop, setPreferNoLanguageForBackdrop] =
		useState(false);
	const [languageOptions, setLanguageOptions] = useState<LanguageOption[]>([]);
	const [languageToAdd, setLanguageToAdd] = useState("");

	async function load(current: Session) {
		const response = await adminFetch("/api/admin/metadata/providers", current);
		if (response.ok) {
			const nextProviders = await response.json();
			setProviders(nextProviders);
			setTmdb(nextProviders.tmdb?.credential || "");
			setTmdbType(nextProviders.tmdb?.credentialType || "api_key");
			setTvdb(nextProviders.tvdb?.credential || "");
		}
		const languageResponse = await adminFetch(
			"/api/admin/metadata/languages",
			current,
		);
		if (languageResponse.ok) {
			const languageData = await languageResponse.json();
			setLocales(languageData.locales || ["en"]);
			setPreferNoLanguageForBackdrop(
				languageData.preferNoLanguageForBackdrop === true,
			);
			setLanguageOptions(languageData.options || []);
		}
	}

	async function saveLanguages() {
		if (!session) return;
		const response = await adminFetch("/api/admin/metadata/languages", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ locales, preferNoLanguageForBackdrop }),
		});
		const data = await response.json().catch(() => null);
		setMessage(
			response.ok
				? "Metadata settings saved; existing metadata backfill queued."
				: data?.detail || "Could not save metadata languages.",
		);
		if (response.ok) {
			setLocales(data.locales || locales);
			setPreferNoLanguageForBackdrop(data.preferNoLanguageForBackdrop === true);
		}
	}

	async function refreshMetadata() {
		if (!session) return;
		const response = await adminFetch("/api/admin/metadata/refresh", session, {
			method: "POST",
		});
		const data = await response.json().catch(() => null);
		setMessage(
			response.ok
				? "Metadata and artwork refresh queued."
				: data?.detail || "Could not queue metadata refresh.",
		);
	}

	function addLanguage() {
		if (!languageToAdd || locales.includes(languageToAdd)) return;
		setLocales((current) => [...current, languageToAdd]);
		setLanguageToAdd("");
	}
	useEffect(() => {
		const current = readSession();
		if (current) {
			setSession(current);
			load(current);
		}
	}, []);

	async function save(event: FormEvent, provider: "tmdb" | "tvdb") {
		event.preventDefault();
		if (!session) return;
		const body =
			provider === "tmdb"
				? { credential: tmdb, credentialType: tmdbType, validate: true }
				: { apiKey: tvdb, pin, validate: true };
		const response = await adminFetch(
			`/api/admin/metadata/providers/${provider}`,
			session,
			{
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(body),
			},
		);
		setMessage(
			response.ok
				? `${provider.toUpperCase()} credentials saved.`
				: (await response.json().catch(() => null))?.detail ||
						"Provider validation failed.",
		);
		if (response.ok) {
			if (provider === "tmdb") setTmdb("");
			else {
				setTvdb("");
				setPin("");
			}
			load(session);
		}
	}

	async function clear(provider: "tmdb" | "tvdb") {
		if (!session) return;
		await adminFetch(`/api/admin/metadata/providers/${provider}`, session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ clear: true }),
		});
		setMessage(`${provider.toUpperCase()} credentials cleared.`);
		load(session);
	}

	return (
		<div className="max-w-6xl">
			<PageHeader
				title="Provider connections"
				description="Choose the metadata languages and services used while library content is indexed."
				actions={
					<button
						onClick={() => session && load(session)}
						className="material-icon-button"
						aria-label="Refresh metadata providers"
						title="Refresh metadata providers"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			<div className="mt-7 grid gap-6 lg:grid-cols-2">
				<section className="console-card rounded-2xl p-6 lg:col-span-2">
					<div className="flex items-start justify-between gap-4">
						<div>
							<p className="console-kicker">Scan-time translations</p>
							<h2 className="mt-2 text-xl font-bold">Metadata languages</h2>
							<p className="mt-3 max-w-3xl text-sm leading-6 console-muted">
								Choose the languages the Orchestrator should collect during library
								scans. Saving queues a backfill for existing indexed items; unavailable
								provider translations are skipped.
							</p>
						</div>
						<IconMusic className="text-[#5ee3d8]" size={22} />
					</div>
					<div className="mt-5 flex flex-wrap gap-2">
						{locales.map((locale) => (
							<span
								key={locale}
								className="flex items-center gap-2 rounded-full border console-divider px-3 py-2 text-sm"
							>
								{locale}
								<button
									type="button"
									disabled={locales.length === 1}
									onClick={() =>
										setLocales((current) => current.filter((value) => value !== locale))
									}
									className="console-muted hover:text-white disabled:opacity-30"
									aria-label={`Remove ${locale}`}
								>
									<IconX size={14} />
								</button>
							</span>
						))}
					</div>
					<div className="mt-5 flex flex-wrap gap-3">
						<select
							value={languageToAdd}
							onChange={(event) => setLanguageToAdd(event.target.value)}
							className="console-input h-11 min-w-56 rounded-xl px-4 text-sm outline-none"
						>
							<option value="">Select a language</option>
							{languageOptions
								.filter((option) => !locales.includes(option.value))
								.map((option) => (
									<option key={option.value} value={option.value}>
										{option.label}
									</option>
								))}
						</select>
						<button
							type="button"
							onClick={addLanguage}
							disabled={!languageToAdd}
							className="flex items-center gap-2 rounded-xl border console-divider px-4 py-3 text-sm font-semibold disabled:opacity-40"
						>
							<IconPlus size={16} />
							Add language
						</button>
						<button
							type="button"
							onClick={saveLanguages}
							className="console-button rounded-xl px-4 py-3 text-sm font-semibold"
						>
							Save languages
						</button>
						<button
							type="button"
							onClick={refreshMetadata}
							className="flex items-center gap-2 rounded-xl border console-divider px-4 py-3 text-sm font-semibold"
						>
							<IconRefresh size={16} />
							Refresh metadata
						</button>
						<label className="flex w-full items-start gap-3 rounded-xl border console-divider p-4 text-sm">
							<input
								type="checkbox"
								checked={preferNoLanguageForBackdrop}
								onChange={(event) =>
									setPreferNoLanguageForBackdrop(event.target.checked)
								}
								className="mt-1 h-4 w-4 accent-[#5ee3d8]"
								aria-describedby="backdrop-language-help"
							/>
							<span>
								<span className="font-semibold">Prefer no language for backdrops</span>
								<span
									id="backdrop-language-help"
									className="mt-1 block leading-5 console-muted"
								>
									When enabled, a backdrop without a language is selected before the
									requested language. Other artwork types keep their current order.
								</span>
							</span>
						</label>
					</div>
				</section>
				<form
					onSubmit={(event) => save(event, "tmdb")}
					className="console-card rounded-2xl p-6"
				>
					<div className="flex items-start justify-between">
						<div>
							<p className="console-kicker">Movies and secondary TV metadata</p>
							<h2 className="mt-2 text-xl font-bold">TMDB</h2>
						</div>
						<IconKey className="text-[#5ee3d8]" size={22} />
					</div>
					<p className="mt-3 text-sm console-muted">
						Use a TMDB v3 API key or v4 read access token.
					</p>
					<select
						value={tmdbType}
						onChange={(event) => setTmdbType(event.target.value)}
						className="console-input mt-5 h-11 w-full rounded-xl px-4 text-sm outline-none"
					>
						<option value="api_key">v3 API key</option>
						<option value="read_access_token">v4 read access token</option>
					</select>
					<input
						value={tmdb}
						onChange={(event) => setTmdb(event.target.value)}
						required={!providers.tmdb?.configured}
						placeholder="Credential"
						type="text"
						className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
					/>
					<ProviderStatus state={providers.tmdb} />
					<div className="mt-5 flex gap-3">
						<button className="console-button rounded-xl px-4 py-3 text-sm font-semibold">
							Save and validate
						</button>
						{providers.tmdb?.configured && (
							<button
								type="button"
								onClick={() => clear("tmdb")}
								className="rounded-xl border console-divider px-4 py-3 text-sm console-muted"
							>
								Clear
							</button>
						)}
					</div>
				</form>
				<form
					onSubmit={(event) => save(event, "tvdb")}
					autoComplete="off"
					className="console-card rounded-2xl p-6"
				>
					<div className="flex items-start justify-between">
						<div>
							<p className="console-kicker">Series and collection metadata</p>
							<h2 className="mt-2 text-xl font-bold">TheTVDB</h2>
						</div>
						<IconKey className="text-[#5ee3d8]" size={22} />
					</div>
					<p className="mt-3 text-sm console-muted">
						A subscriber PIN is optional for licensed keys and required for some
						user-supported keys.
					</p>
					<input
						name="tvdb-api-key"
						autoComplete="off"
						value={tvdb}
						onChange={(event) => setTvdb(event.target.value)}
						required={!providers.tvdb?.configured}
						placeholder="v4 API key"
						type="text"
						className="console-input mt-5 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
					/>
					<input
						name="tvdb-subscriber-pin"
						autoComplete="one-time-code"
						value={pin}
						onChange={(event) => setPin(event.target.value)}
						placeholder="Subscriber PIN (optional)"
						type="password"
						className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
					/>
					<ProviderStatus state={providers.tvdb} />
					<div className="mt-5 flex gap-3">
						<button className="console-button rounded-xl px-4 py-3 text-sm font-semibold">
							Save and validate
						</button>
						{providers.tvdb?.configured && (
							<button
								type="button"
								onClick={() => clear("tvdb")}
								className="rounded-xl border console-divider px-4 py-3 text-sm console-muted"
							>
								Clear
							</button>
						)}
					</div>
				</form>
				<div className="console-card rounded-2xl p-6 lg:col-span-2">
					<div className="flex items-start gap-4">
						<span className="rounded-xl bg-[#5ee3d8]/10 p-3 text-[#5ee3d8]">
							<IconMusic size={22} />
						</span>
						<div>
							<p className="console-kicker">Music</p>
							<h2 className="mt-2 text-xl font-bold">
								MusicBrainz and Cover Art Archive
							</h2>
							<p className="mt-2 text-sm leading-6 console-muted">
								MusicBrainz does not require an API key. ZenStream identifies requests
								with its application user agent and uses local artwork before querying
								the Cover Art Archive.
							</p>
							<p className="mt-4 text-xs console-muted">
								Metadata provided by{" "}
								<a
									className="text-[#5ee3d8]"
									href="https://musicbrainz.org/"
									target="_blank"
									rel="noreferrer"
								>
									MusicBrainz
								</a>{" "}
								and artwork by the{" "}
								<a
									className="text-[#5ee3d8]"
									href="https://coverartarchive.org/"
									target="_blank"
									rel="noreferrer"
								>
									Cover Art Archive
								</a>
								.
							</p>
						</div>
					</div>
				</div>
			</div>
			{message && <StatusMessage>{message}</StatusMessage>}
		</div>
	);
}

function ProviderStatus({ state }: { state?: ProviderState }) {
	return (
		<p className="mt-4 flex items-center gap-2 text-xs console-muted">
			{state?.configured ? (
				<>
					<IconCheck size={15} className="text-[#5ee3d8]" />
					Configured
					{state.validatedAt
						? ` · validated ${new Date(state.validatedAt).toLocaleString()}`
						: ""}
				</>
			) : (
				<>
					<IconX size={15} />
					Not configured
				</>
			)}
		</p>
	);
}
