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
type ArtworkSetting = { enabled: boolean; maxAgeDays: number };
type RefreshItemSetting = {
	enabled: boolean;
	cooldownMinutes: number;
	cutoffDays: number;
	minimumProviderIds: number;
	checks: {
		missingTitle: boolean;
		missingOverview: boolean;
		missingName: boolean;
		nameIsDate: boolean;
		overviewContainsBadName: boolean;
	};
	statusAfterDays: number;
	documentMaxAgeDays: number;
	artwork: Record<string, ArtworkSetting>;
	replaceAllMetadata: boolean;
	replaceAllImages: boolean;
};
type RefreshSettings = {
	seriesBlockList: string;
	badNames: string;
	pretend: boolean;
	itemTypes: Record<string, RefreshItemSetting>;
};

const artworkTypes = ["Primary", "Backdrop", "Logo", "Banner"];
const refreshItemLabels: Record<string, string> = {
	movie: "Movies",
	series: "Series",
	season: "Seasons",
	episode: "Episodes",
};
const refreshCheckLabels: Record<string, string> = {
	missingTitle: "Missing title",
	missingOverview: "Missing overview",
	missingName: "Missing or placeholder name",
	nameIsDate: "Name is only a date",
	overviewContainsBadName: "Overview contains a blocked name",
};

function makeArtwork(enabled: string[] = []): Record<string, ArtworkSetting> {
	return Object.fromEntries(
		artworkTypes.map((imageType) => [
			imageType,
			{ enabled: enabled.includes(imageType), maxAgeDays: 7 },
		]),
	);
}

function makeRefreshItem(
	cooldownMinutes: number,
	cutoffDays: number,
	checks: Partial<RefreshItemSetting["checks"]>,
	artwork: string[],
	statusAfterDays = -1,
): RefreshItemSetting {
	return {
		enabled: true,
		cooldownMinutes,
		cutoffDays,
		minimumProviderIds: 0,
		checks: {
			missingTitle: false,
			missingOverview: false,
			missingName: false,
			nameIsDate: false,
			overviewContainsBadName: false,
			...checks,
		},
		statusAfterDays,
		documentMaxAgeDays: 7,
		artwork: makeArtwork(artwork),
		replaceAllMetadata: false,
		replaceAllImages: false,
	};
}

const defaultRefreshSettings: RefreshSettings = {
	seriesBlockList: "",
	badNames: "",
	pretend: false,
	itemTypes: {
		movie: makeRefreshItem(
			43200,
			-1,
			{
				missingTitle: true,
				missingOverview: true,
			},
			["Primary"],
		),
		series: makeRefreshItem(
			43200,
			-1,
			{ missingTitle: true, missingOverview: true },
			["Primary", "Backdrop"],
			180,
		),
		season: makeRefreshItem(43200, -1, { missingOverview: true }, ["Primary"]),
		episode: makeRefreshItem(
			60,
			14,
			{
				missingTitle: true,
				missingOverview: true,
				missingName: true,
				nameIsDate: true,
				overviewContainsBadName: true,
			},
			["Primary"],
		),
	},
};

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
	const [refreshSettings, setRefreshSettings] = useState<RefreshSettings>(
		defaultRefreshSettings,
	);
	const [refreshAll, setRefreshAll] = useState(false);

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
		const refreshResponse = await adminFetch(
			"/api/admin/metadata/refresh/settings",
			current,
		);
		if (refreshResponse.ok) {
			const refreshData = await refreshResponse.json();
			if (refreshData?.itemTypes) setRefreshSettings(refreshData);
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
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ refreshAll }),
		});
		const data = await response.json().catch(() => null);
		setMessage(
			response.ok
				? "Metadata and artwork refresh queued."
				: data?.detail || "Could not queue metadata refresh.",
		);
	}

	async function saveRefreshSettings() {
		if (!session) return;
		const response = await adminFetch(
			"/api/admin/metadata/refresh/settings",
			session,
			{
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(refreshSettings),
			},
		);
		const data = await response.json().catch(() => null);
		setMessage(
			response.ok
				? "Sparse metadata refresh settings saved."
				: data?.detail || "Could not save refresh settings.",
		);
		if (response.ok && data?.itemTypes) setRefreshSettings(data);
	}

	function updateRefreshItem(
		itemType: string,
		change: (item: RefreshItemSetting) => RefreshItemSetting,
	) {
		setRefreshSettings((current) => ({
			...current,
			itemTypes: {
				...current.itemTypes,
				[itemType]: change(current.itemTypes[itemType]),
			},
		}));
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
							<h2 className="text-xl font-bold">Metadata languages</h2>
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
				<section className="console-card rounded-2xl p-6 lg:col-span-2">
					<div className="flex flex-wrap items-start justify-between gap-4">
						<div>
							<h2 className="text-xl font-bold">Sparse refresh settings</h2>
							<p className="mt-3 max-w-3xl text-sm leading-6 console-muted">
								Scheduled refreshes only revisit items that match a selected missing or
								placeholder check and whose relevant provider cache is older than the
								configured age. Cooldowns are measured from the last attempt.
							</p>
						</div>
						<div className="flex items-center gap-2 text-xs console-muted">
							<span className="rounded-full bg-[#5ee3d8]/10 px-3 py-1 text-[#5ee3d8]">
								Sparse by default
							</span>
							<a className="text-[#5ee3d8] hover:underline" href="/web/dashboard/jobs">
								Configure schedule in Jobs
							</a>
						</div>
					</div>
					<div className="mt-6 grid gap-4 lg:grid-cols-2">
						<label className="flex items-start gap-3 rounded-xl border console-divider p-4 text-sm">
							<input
								type="checkbox"
								checked={refreshSettings.pretend}
								onChange={(event) =>
									setRefreshSettings((current) => ({
										...current,
										pretend: event.target.checked,
									}))
								}
								className="mt-1 h-4 w-4 accent-[#5ee3d8]"
							/>
							<span>
								<span className="font-semibold">Preview only</span>
								<span className="mt-1 block leading-5 console-muted">
									Log the items that would be refreshed without contacting providers or
									updating cooldown state.
								</span>
							</span>
						</label>
						<label className="text-sm">
							<span className="font-semibold">Series block list</span>
							<span className="mt-1 block text-xs console-muted">
								Pipe, comma, or newline separated title, path, or provider-ID matches.
							</span>
							<textarea
								value={refreshSettings.seriesBlockList}
								onChange={(event) =>
									setRefreshSettings((current) => ({
										...current,
										seriesBlockList: event.target.value,
									}))
								}
								className="console-input mt-2 min-h-20 w-full rounded-xl px-4 py-3 text-sm outline-none"
								placeholder="Example Series | 12345"
							/>
						</label>
						<label className="text-sm lg:col-span-2">
							<span className="font-semibold">Bad names and overview text</span>
							<span className="mt-1 block text-xs console-muted">
								Episode checks use these values for name prefixes and overview contains
								matches.
							</span>
							<textarea
								value={refreshSettings.badNames}
								onChange={(event) =>
									setRefreshSettings((current) => ({
										...current,
										badNames: event.target.value,
									}))
								}
								className="console-input mt-2 min-h-20 w-full rounded-xl px-4 py-3 text-sm outline-none"
								placeholder="Example: TBA | TBD | Series Name"
							/>
						</label>
					</div>
					<div className="mt-6 space-y-3">
						{Object.entries(refreshItemLabels).map(([itemType, label]) => {
							const item = refreshSettings.itemTypes[itemType];
							if (!item) return null;
							return (
								<details
									key={itemType}
									open
									className="rounded-xl border console-divider"
								>
									<summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-4 text-sm font-semibold">
										<span>{label}</span>
										<label
											className="flex items-center gap-2 text-xs font-normal console-muted"
											onClick={(event) => event.stopPropagation()}
										>
											<input
												type="checkbox"
												checked={item.enabled}
												onChange={(event) =>
													updateRefreshItem(itemType, (current) => ({
														...current,
														enabled: event.target.checked,
													}))
												}
												className="h-4 w-4 accent-[#5ee3d8]"
											/>
											Enabled
										</label>
									</summary>
									<div className="grid gap-4 border-t console-divider px-4 py-4 md:grid-cols-2 xl:grid-cols-4">
										<NumberSetting
											label="Cooldown (minutes)"
											help="Wait this many minutes between attempts for the same item. Use -1 for no cooldown."
											value={item.cooldownMinutes}
											onChange={(value) =>
												updateRefreshItem(itemType, (current) => ({
													...current,
													cooldownMinutes: value,
												}))
											}
										/>
										<NumberSetting
											label="Catalog cutoff (days)"
											help="Only refresh items added to the catalog within this many days. Use -1 to include older items."
											value={item.cutoffDays}
											onChange={(value) =>
												updateRefreshItem(itemType, (current) => ({
													...current,
													cutoffDays: value,
												}))
											}
										/>
										<NumberSetting
											label="Minimum provider IDs"
											help="Require at least this many TMDB or TVDB IDs before refreshing. Use 0 for no minimum."
											value={item.minimumProviderIds}
											onChange={(value) =>
												updateRefreshItem(itemType, (current) => ({
													...current,
													minimumProviderIds: Math.max(0, value),
												}))
											}
										/>
										<NumberSetting
											label="Provider document age (days)"
											help="Fetch provider metadata only when its cache is this many days old. Use -1 to ignore cache age."
											value={item.documentMaxAgeDays}
											onChange={(value) =>
												updateRefreshItem(itemType, (current) => ({
													...current,
													documentMaxAgeDays: value,
												}))
											}
										/>
										{itemType === "series" && (
											<NumberSetting
												label="Status refresh age (days)"
												help="For ongoing series, refresh metadata and status after this many days. Use -1 to disable this check."
												value={item.statusAfterDays}
												onChange={(value) =>
													updateRefreshItem(itemType, (current) => ({
														...current,
														statusAfterDays: value,
													}))
												}
											/>
										)}
										<div className="md:col-span-2 xl:col-span-4">
											<p className="text-xs font-semibold uppercase tracking-[0.12em] console-muted">
												Sparse checks
											</p>
											<div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
												{(
													Object.keys(item.checks) as Array<
														keyof RefreshItemSetting["checks"]
													>
												).map((check) => (
													<label
														key={check}
														className="flex items-start gap-2 text-xs console-muted"
													>
														<input
															type="checkbox"
															checked={item.checks[check]}
															onChange={(event) =>
																updateRefreshItem(itemType, (current) => ({
																	...current,
																	checks: {
																		...current.checks,
																		[check]: event.target.checked,
																	},
																}))
															}
															className="mt-0.5 h-4 w-4 accent-[#5ee3d8]"
														/>
														<span>{refreshCheckLabels[check]}</span>
													</label>
												))}
											</div>
										</div>
										<div className="md:col-span-2 xl:col-span-4">
											<p className="text-xs font-semibold uppercase tracking-[0.12em] console-muted">
												Artwork buckets
											</p>
											<div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
												{artworkTypes.map((imageType) => (
													<div
														key={imageType}
														className="rounded-xl border console-divider p-3"
													>
														<label className="flex items-center gap-2 text-xs font-semibold">
															<input
																type="checkbox"
																checked={item.artwork[imageType]?.enabled === true}
																onChange={(event) =>
																	updateRefreshItem(itemType, (current) => ({
																		...current,
																		artwork: {
																			...current.artwork,
																			[imageType]: {
																				...current.artwork[imageType],
																				enabled: event.target.checked,
																			},
																		},
																	}))
																}
																className="h-4 w-4 accent-[#5ee3d8]"
															/>
															{imageType}
														</label>
														<input
															type="number"
															value={item.artwork[imageType]?.maxAgeDays ?? 7}
															onChange={(event) =>
																updateRefreshItem(itemType, (current) => ({
																	...current,
																	artwork: {
																		...current.artwork,
																		[imageType]: {
																			...current.artwork[imageType],
																			maxAgeDays: Number(event.target.value),
																		},
																	},
																}))
															}
															className="console-input mt-3 h-9 w-full rounded-lg px-3 text-xs outline-none"
															aria-label={`${imageType} artwork maximum age in days`}
														/>
														<p className="mt-1 text-[11px] console-muted">
															-1 disables age gate
														</p>
													</div>
												))}
											</div>
										</div>
										<label className="flex items-start gap-2 text-xs console-muted">
											<input
												type="checkbox"
												checked={item.replaceAllMetadata}
												onChange={(event) =>
													updateRefreshItem(itemType, (current) => ({
														...current,
														replaceAllMetadata: event.target.checked,
													}))
												}
												className="mt-0.5 h-4 w-4 accent-[#5ee3d8]"
											/>
											<span>
												<span className="font-semibold text-white">
													Overwrite existing provider metadata
												</span>
												<span className="mt-1 block leading-5 console-muted">
													Use fresh TMDB/TVDB values for populated titles, overviews, dates,
													genres, ratings, and other provider fields.
												</span>
											</span>
										</label>
										<label className="flex items-start gap-2 text-xs console-muted">
											<input
												type="checkbox"
												checked={item.replaceAllImages}
												onChange={(event) =>
													updateRefreshItem(itemType, (current) => ({
														...current,
														replaceAllImages: event.target.checked,
													}))
												}
												className="mt-0.5 h-4 w-4 accent-[#5ee3d8]"
											/>
											<span>
												<span className="font-semibold text-white">
													Redownload provider artwork
												</span>
												<span className="mt-1 block leading-5 console-muted">
													Fetch fresh provider image files even when their URLs have not
													changed. Local library artwork is not modified.
												</span>
											</span>
										</label>
									</div>
								</details>
							);
						})}
					</div>
					<div className="mt-6 flex flex-wrap items-center gap-3">
						<button
							type="button"
							onClick={saveRefreshSettings}
							className="console-button rounded-xl px-4 py-3 text-sm font-semibold"
						>
							Save sparse settings
						</button>
						<label className="flex items-center gap-2 rounded-xl border console-divider px-4 py-3 text-sm">
							<input
								type="checkbox"
								checked={refreshAll}
								onChange={(event) => setRefreshAll(event.target.checked)}
								className="h-4 w-4 accent-[#5ee3d8]"
							/>
							Full refresh for the next manual run
						</label>
						<button
							type="button"
							onClick={refreshMetadata}
							className="flex items-center gap-2 rounded-xl border console-divider px-4 py-3 text-sm font-semibold"
						>
							<IconRefresh size={16} />
							{refreshAll ? "Run full refresh" : "Run sparse refresh"}
						</button>
					</div>
				</section>
				<form
					onSubmit={(event) => save(event, "tmdb")}
					className="console-card rounded-2xl p-6"
				>
					<div className="flex items-start justify-between">
						<div>
							<h2 className="text-xl font-bold">TMDB</h2>
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
							<h2 className="text-xl font-bold">TheTVDB</h2>
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
							<h2 className="text-xl font-bold">MusicBrainz and Cover Art Archive</h2>
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

function NumberSetting({
	label,
	help,
	value,
	onChange,
}: {
	label: string;
	help: string;
	value: number;
	onChange: (value: number) => void;
}) {
	return (
		<label className="text-xs">
			<span className="font-semibold">{label}</span>
			<input
				type="number"
				value={value}
				onChange={(event) => onChange(Number(event.target.value))}
				className="console-input mt-2 h-10 w-full rounded-lg px-3 text-sm outline-none"
			/>
			<span className="mt-1 block console-muted">{help}</span>
		</label>
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
