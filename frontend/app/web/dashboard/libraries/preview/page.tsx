"use client";

import Link from "next/link";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
	IconAlertCircle,
	IconArrowLeft,
	IconChevronLeft,
	IconChevronRight,
	IconPlayerPlay,
	IconPhoto,
	IconRefresh,
	IconSearch,
	IconX,
} from "@tabler/icons-react";
import {
	adminFetch,
	readSession,
	Session,
} from "../../components/admin-client";

type Child = {
	id: string;
	type: string;
	displayName?: string;
	relativePath?: string;
	seasonNumber?: number;
	episodeNumber?: number;
	trackNumber?: number;
};

type Item = {
	id: string;
	type: string;
	displayName: string;
	relativePath?: string;
	parentId?: string | null;
	metadata?: Metadata | null;
	matchStatus: string;
	providerIds: {
		provider: string;
		id: string;
		primary?: boolean;
		role?: string;
	}[];
	primaryProvider?: string | null;
	children?: Child[];
	trickplay?: TrickplayAsset | null;
	revision?: string;
};

type TrickplayAsset = {
	mediaFileId: string;
	frameWidth: number;
	frameHeight: number;
	intervalSeconds: number;
	state: "queued" | "generating" | "ready" | "failed" | string;
	generation?: string | null;
	error?: string | null;
	frameCount: number;
	sheets: { index: number; frameCount: number }[];
};

type IntroOutroInspection = {
	sourceId?: string;
	durationSeconds?: number;
	state: string;
	error?: string | null;
	updatedAt?: string | null;
	segments: {
		type: "intro" | "outro";
		startSeconds: number;
		endSeconds: number;
	}[];
	fingerprints: {
		type: "intro" | "outro";
		startSeconds: number;
		endSeconds: number;
		pointCount: number;
		sampleSeconds: number;
		values: number[];
	}[];
};

type MetadataPerson = { name?: string; role?: string };
type MetadataTrailer = {
	url?: string;
	name?: string;
	key?: string;
	language?: string;
};
type MetadataTrack = { title?: string };
type Metadata = {
	title?: string;
	description?: string;
	overview?: string;
	status?: string;
	date?: string;
	releaseDate?: string;
	runtimeMinutes?: number;
	airTime?: string;
	tags?: string[];
	genres?: string[];
	studios?: string[];
	networks?: string[];
	originalCountry?: string;
	originalLanguage?: string;
	albumArtist?: string;
	trailers?: MetadataTrailer[];
	people?: MetadataPerson[];
	tracks?: MetadataTrack[];
};

type NavigationEntry = { id: string; type: string; label: string };
type Navigable = {
	id: string;
	type: string;
	displayName?: string;
	relativePath?: string;
};
type CardItem = Navigable & {
	metadata?: Metadata | null;
	matchStatus?: string;
	seasonNumber?: number;
	episodeNumber?: number;
	revision?: string;
};
type LanguageOption = { value: string; label: string };
type ArtworkType = "Primary" | "Backdrop" | "Logo" | "Banner";
const artworkTypes: ArtworkType[] = ["Primary", "Backdrop", "Logo", "Banner"];
const pageSize = 30;

function LibraryViewPage() {
	const params = useSearchParams();
	const router = useRouter();
	const pathname = usePathname();
	const libraryId = params.get("libraryId") || "";
	const urlEntityId = params.get("entityId");
	const urlEntityPathValue = params.get("entityPath") || "";
	const urlLocale = params.get("locale");
	const urlQuery = params.get("query") || "";
	const parsedUrlPage = Number(params.get("page") || "1");
	const [session, setSession] = useState<Session | null>(null);
	const [items, setItems] = useState<Item[]>([]);
	const [libraryName, setLibraryName] = useState("");
	const [parent, setParent] = useState<string | null>(null);
	const [locale, setLocale] = useState("");
	const [locales, setLocales] = useState<string[]>([]);
	const [languageOptions, setLanguageOptions] = useState<LanguageOption[]>([]);
	const [searchInput, setSearchInput] = useState(urlQuery);
	const [searchQuery, setSearchQuery] = useState(urlQuery);
	const [page, setPage] = useState(
		Number.isFinite(parsedUrlPage) && parsedUrlPage > 0 ? parsedUrlPage : 1,
	);
	const [total, setTotal] = useState(0);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [navigation, setNavigation] = useState<NavigationEntry[]>([]);
	const requestId = useRef(0);
	const abortRef = useRef<AbortController | null>(null);
	const catalogGeneration = useRef<number | null>(null);
	const backgroundRefresh = useRef(false);
	const trailingRefresh = useRef(false);
	const loadRef = useRef<
		| ((
				current: Session | null,
				parentId: string | null,
				currentPage: number,
				background?: boolean,
		  ) => Promise<void>)
		| null
	>(null);

	function mergeItems(previous: Item[], next: Item[]) {
		const byId = new Map(previous.map((item) => [item.id, item]));
		return next.map((item) => {
			const old = byId.get(item.id);
			return old && old.revision === item.revision ? old : item;
		});
	}

	const load = useCallback(
		async (
			current: Session | null,
			parentId: string | null,
			currentPage: number,
			background = false,
		) => {
			if (!current || !libraryId || !locale) return;
			const id = ++requestId.current;
			abortRef.current?.abort();
			const controller = new AbortController();
			abortRef.current = controller;
			setError("");
			if (!background) setLoading(true);
			try {
				const init = { signal: controller.signal };
				const [libraryResponse, itemsResponse] = await Promise.all([
					adminFetch("/api/admin/libraries/" + libraryId, current, init),
					adminFetch(
						"/api/admin/libraries/" +
							libraryId +
							"/items?parentId=" +
							encodeURIComponent(parentId || "") +
							"&locale=" +
							encodeURIComponent(locale) +
							"&query=" +
							encodeURIComponent(searchQuery) +
							"&page=" +
							currentPage +
							"&pageSize=" +
							pageSize,
						current,
						init,
					),
				]);
				if (id !== requestId.current) return;
				if (!libraryResponse.ok || !itemsResponse.ok)
					throw new Error("The library could not be loaded.");
				const [library, value] = await Promise.all([
					libraryResponse.json(),
					itemsResponse.json(),
				]);
				if (id !== requestId.current) return;
				setLibraryName(library.name || "Library");
				setItems((currentItems) =>
					background
						? mergeItems(currentItems, value.items || [])
						: value.items || [],
				);
				setTotal(value.total || 0);
				if (typeof value.catalogGeneration === "number")
					catalogGeneration.current = value.catalogGeneration;
			} catch (caught) {
				if ((caught as Error).name !== "AbortError" && id === requestId.current)
					setError(
						caught instanceof Error
							? caught.message
							: "The library could not be loaded.",
					);
			} finally {
				if (id === requestId.current && !background) setLoading(false);
			}
		},
		[libraryId, locale, searchQuery],
	);
	loadRef.current = load;

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (!current || !libraryId) return;
		let cancelled = false;
		void adminFetch("/api/admin/metadata/languages", current)
			.then(async (response) => {
				if (!response.ok)
					throw new Error("Metadata languages could not be loaded.");
				return response.json();
			})
			.then((value) => {
				if (cancelled) return;
				const configured = Array.isArray(value.locales)
					? value.locales.filter(
							(item: unknown): item is string =>
								typeof item === "string" && item.length > 0,
						)
					: [];
				setLocales(configured);
				setLanguageOptions(Array.isArray(value.options) ? value.options : []);
				const storedLocale = window.localStorage.getItem(
					"zenstream.admin.metadataLocale",
				);
				const preferredLocale =
					urlLocale && configured.includes(urlLocale)
						? urlLocale
						: storedLocale && configured.includes(storedLocale)
							? storedLocale
							: "";
				setLocale(
					(currentLocale) =>
						preferredLocale ||
						(currentLocale && configured.includes(currentLocale)
							? currentLocale
							: configured[0] || ""),
				);
			})
			.catch((caught) => {
				if (!cancelled)
					setError(
						caught instanceof Error
							? caught.message
							: "Metadata languages could not be loaded.",
					);
			});
		return () => {
			cancelled = true;
		};
	}, [libraryId, urlLocale]);

	useEffect(() => {
		const timer = window.setTimeout(() => {
			const nextQuery = searchInput.trim();
			setSearchQuery(nextQuery);
			if (navigation.length === 0 && (params.get("query") || "") !== nextQuery) {
				setPage(1);
				const next = new URLSearchParams(params.toString());
				if (nextQuery) next.set("query", nextQuery);
				else next.delete("query");
				next.delete("page");
				const nextHref = `${pathname}${next.toString() ? `?${next.toString()}` : ""}`;
				router.replace(nextHref);
			}
		}, 300);
		return () => window.clearTimeout(timer);
	}, [navigation.length, params, pathname, router, searchInput]);

	useEffect(() => {
		const current = readSession();
		setSession(current);
		setPage(1);
		setParent(null);
		setNavigation([]);
		setItems([]);
		return () => abortRef.current?.abort();
	}, [libraryId]);

	useEffect(() => {
		if (session && locale && navigation.length === 0)
			void load(session, parent, page, false);
	}, [locale, load, navigation.length, page, parent, session]);

	useEffect(() => {
		const ids = urlEntityPathValue.split(",").filter(Boolean);
		if (!urlEntityId && !ids.length) {
			setNavigation([]);
			return;
		}
		const path = ids.length ? ids : [urlEntityId as string];
		setNavigation((current) =>
			current.length && current[current.length - 1].id === path[path.length - 1]
				? current
				: path.map((id) => ({ id, type: "item", label: "Library item" })),
		);
	}, [urlEntityId, urlEntityPathValue]);

	useEffect(() => {
		if (!session || !locale || !libraryId || navigation.length !== 0) return;
		let cancelled = false;
		let timer: number | undefined;
		const refresh = async () => {
			if (document.visibilityState !== "visible") {
				timer = window.setTimeout(refresh, 2000);
				return;
			}
			try {
				const response = await adminFetch(
					`/api/admin/libraries/${libraryId}/catalog-status`,
					session,
					{ cache: "no-store" },
				);
				if (!response.ok) throw new Error("Catalog status unavailable.");
				const status = await response.json();
				const generation = Number(status.catalogGeneration || 0);
				if (catalogGeneration.current == null)
					catalogGeneration.current = generation;
				else if (generation > catalogGeneration.current) {
					catalogGeneration.current = generation;
					if (backgroundRefresh.current) trailingRefresh.current = true;
					else {
						backgroundRefresh.current = true;
						await loadRef.current?.(session, parent, page, true);
						backgroundRefresh.current = false;
						if (trailingRefresh.current) {
							trailingRefresh.current = false;
							backgroundRefresh.current = true;
							await loadRef.current?.(session, parent, page, true);
							backgroundRefresh.current = false;
						}
					}
				}
			} catch {
				// The current grid remains usable while status polling is unavailable.
			}
			if (!cancelled) timer = window.setTimeout(refresh, 2000);
		};
		void refresh();
		return () => {
			cancelled = true;
			if (timer) window.clearTimeout(timer);
		};
	}, [libraryId, locale, navigation.length, page, parent, session]);

	useEffect(() => {
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.key === "Escape" && navigation.length > 0)
				setNavigation((current) => current.slice(0, -1));
		};
		window.addEventListener("keydown", onKeyDown);
		return () => window.removeEventListener("keydown", onKeyDown);
	}, [navigation.length]);

	function openItem(item: Navigable) {
		const nextEntry = {
			id: item.id,
			type: item.type,
			label: item.displayName || item.relativePath || item.type,
		};
		const nextStack =
			navigation[navigation.length - 1]?.id === item.id
				? navigation
				: [...navigation, nextEntry];
		setNavigation(nextStack);
		const next = new URLSearchParams(params.toString());
		next.set("entityId", item.id);
		next.set("entityPath", nextStack.map((value) => value.id).join(","));
		router.push(`${pathname}?${next.toString()}`);
	}

	function back() {
		const previous = navigation.slice(0, -1);
		setNavigation(previous);
		const next = new URLSearchParams(params.toString());
		if (previous.length) {
			next.set("entityId", previous[previous.length - 1].id);
			next.set("entityPath", previous.map((value) => value.id).join(","));
		} else {
			next.delete("entityId");
			next.delete("entityPath");
		}
		router.replace(`${pathname}${next.toString() ? `?${next.toString()}` : ""}`);
	}

	const pageCount = Math.max(1, Math.ceil(total / pageSize));
	if (!libraryId)
		return (
			<div className="dashboard-card p-8 text-sm material-muted">
				Choose a library from the Libraries tab.
			</div>
		);
	return (
		<div className="dashboard-page">
			{navigation.length === 0 ? (
				<>
					<div className="dashboard-page-header material-topbar">
						<div>
							<Link href="/web/dashboard/libraries" className="material-back">
								<IconArrowLeft size={15} />
								Libraries
							</Link>
							<div className="mt-4 flex items-center gap-3">
								<h1 className="text-3xl font-semibold tracking-tight">
									{libraryName || "Library"}
								</h1>
								<button
									onClick={() => load(session, parent, page)}
									className="material-icon-button"
									aria-label="Refresh view"
									title="Refresh view"
								>
									<IconRefresh size={17} />
								</button>
							</div>
							<p className="mt-2 text-sm material-muted">
								{total.toLocaleString()} {searchQuery ? "matching" : "indexed"} entries
							</p>
						</div>
						<div className="dashboard-toolbar flex items-center gap-3">
							<label className="material-input flex h-11 min-w-0 flex-1 items-center gap-2 rounded-lg px-3 sm:min-w-64">
								<IconSearch size={16} className="material-muted" />
								<input
									value={searchInput}
									onChange={(event) => setSearchInput(event.target.value)}
									placeholder="Search library"
									aria-label="Search library"
									className="min-w-0 flex-1 bg-transparent text-sm outline-none"
								/>
								{searchInput && (
									<button
										type="button"
										onClick={() => setSearchInput("")}
										aria-label="Clear search"
										className="material-muted transition hover:text-white"
									>
										<IconX size={15} />
									</button>
								)}
							</label>
							<select
								value={locale}
								disabled={!locales.length}
								onChange={(event) => {
									const nextLocale = event.target.value;
									setLocale(nextLocale);
									window.localStorage.setItem(
										"zenstream.admin.metadataLocale",
										nextLocale,
									);
									const next = new URLSearchParams(params.toString());
									next.set("locale", nextLocale);
									next.delete("page");
									router.replace(`${pathname}?${next.toString()}`);
									setItems([]);
								}}
								className="material-input h-11 rounded-lg px-3 text-sm outline-none"
								aria-label="Metadata language"
							>
								<option value="" disabled>
									Select language
								</option>
								{locales.map((value) => (
									<option key={value} value={value}>
										{languageOptions.find((option) => option.value === value)?.label ||
											value}
									</option>
								))}
							</select>
						</div>
					</div>
					{parent && (
						<button
							onClick={() => {
								setParent(null);
								setPage(1);
								void load(session, null, 1);
							}}
							className="material-back mt-5"
						>
							<IconArrowLeft size={15} />
							Back to library
						</button>
					)}
					{error && (
						<div className="dashboard-alert material-alert mt-6" role="alert">
							<IconAlertCircle size={18} />
							<span className="flex-1">{error}</span>
							<button
								onClick={() => void load(session, parent, page)}
								className="material-text-button"
							>
								Retry
							</button>
						</div>
					)}
					{loading && !items.length && !error && (
						<div className="dashboard-card mt-7 p-8 text-sm material-muted">
							Loading entries…
						</div>
					)}
					{!error && (
						<>
							<div
								className="mt-7 grid w-full gap-4"
								style={{
									gridTemplateColumns: "repeat(auto-fill, minmax(150px, 220px))",
								}}
							>
								{items.map((item) => (
									<EntityCard
										key={item.id}
										item={item}
										session={session}
										locale={locale}
										revision={item.revision}
										onOpen={() => openItem(item)}
									/>
								))}
								{!items.length && (
									<div className="dashboard-card col-span-full p-8 text-sm material-muted">
										{searchQuery ? "No matching entries." : "No entries on this page."}
									</div>
								)}
							</div>
							<div className="mt-6 flex items-center justify-between pt-4">
								<span className="text-xs material-muted">
									Page {page} of {pageCount}
								</span>
								<div className="flex gap-2">
									<button
										disabled={page <= 1}
										onClick={() => {
											const nextPage = page - 1;
											setPage(nextPage);
											const next = new URLSearchParams(params.toString());
											next.set("page", String(nextPage));
											router.push(`${pathname}?${next.toString()}`);
										}}
										className="material-icon-button disabled:opacity-30"
										aria-label="Previous page"
									>
										<IconChevronLeft size={16} />
									</button>
									<button
										disabled={page >= pageCount}
										onClick={() => {
											const nextPage = page + 1;
											setPage(nextPage);
											const next = new URLSearchParams(params.toString());
											next.set("page", String(nextPage));
											router.push(`${pathname}?${next.toString()}`);
										}}
										className="material-icon-button disabled:opacity-30"
										aria-label="Next page"
									>
										<IconChevronRight size={16} />
									</button>
								</div>
							</div>
						</>
					)}
				</>
			) : session ? (
				<EntityDetailView
					entry={navigation[navigation.length - 1]}
					session={session}
					locale={locale}
					onBack={back}
					onOpen={openItem}
				/>
			) : null}
		</div>
	);
}

function EntityCard({
	item,
	session,
	locale,
	revision,
	onOpen,
}: {
	item: CardItem;
	session: Session | null;
	locale: string;
	revision?: string;
	onOpen: () => void;
}) {
	const label = item.displayName || item.relativePath || item.type;
	const metadataPending = !item.metadata;
	return (
		<button
			type="button"
			onClick={onOpen}
			className="material-surface group w-full max-w-[220px] overflow-hidden rounded-xl text-left transition hover:-translate-y-0.5 hover:bg-[#282828] hover:shadow-xl hover:shadow-black/30"
		>
			<EntityPoster
				entityId={item.id}
				session={session}
				locale={locale}
				revision={revision}
				alt={metadataPending ? "" : item.metadata?.title || label}
			/>
			<div className="p-3">
				{metadataPending ? (
					<div
						className="h-4 w-3/4 animate-pulse rounded bg-white/10"
						aria-hidden="true"
					/>
				) : (
					<p className="truncate text-sm font-medium">
						{item.metadata?.title || label}
					</p>
				)}
				<p className="mt-1 truncate text-[11px] material-muted">
					{item.type === "episode" && item.seasonNumber != null
						? `S${item.seasonNumber}E${item.episodeNumber ?? "—"}`
						: item.type}{" "}
					· {item.matchStatus || "ready"}
				</p>
			</div>
		</button>
	);
}

function EpisodeCard({
	item,
	session,
	locale,
	onOpen,
}: {
	item: Child;
	session: Session;
	locale: string;
	onOpen: () => void;
}) {
	const label =
		item.displayName ||
		item.relativePath ||
		`Episode ${item.episodeNumber ?? "—"}`;
	return (
		<button
			type="button"
			onClick={onOpen}
			className="material-surface group flex min-w-0 w-full max-w-[42rem] overflow-hidden rounded-xl text-left transition hover:-translate-y-0.5 hover:bg-[#282828] hover:shadow-xl hover:shadow-black/30"
		>
			<div className="w-36 shrink-0 sm:w-44">
				<EntityPoster
					entityId={item.id}
					session={session}
					locale={locale}
					alt=""
					landscape
				/>
			</div>
			<div className="min-w-0 flex-1 self-center p-4">
				<p className="truncate text-sm font-semibold">{label}</p>
				<p className="mt-2 text-xs material-muted">
					S{item.seasonNumber ?? "—"}E{item.episodeNumber ?? "—"}
				</p>
				<p className="mt-1 text-[11px] material-muted">Open episode details</p>
			</div>
		</button>
	);
}

function EntityPoster({
	entityId,
	session,
	locale,
	revision,
	alt,
	imageType = "Primary",
	landscape = false,
}: {
	entityId: string;
	session: Session | null;
	locale: string;
	revision?: string;
	alt: string;
	imageType?: ArtworkType;
	landscape?: boolean;
}) {
	const [url, setUrl] = useState<string | null>(null);
	const [state, setState] = useState<"loading" | "ready" | "missing">("loading");
	useEffect(() => {
		if (!session) return;
		// Do not display the previous locale's artwork while the new request is pending.
		setUrl(null);
		setState("loading");
		const controller = new AbortController();
		let objectUrl = "";
		let timer: number | undefined;
		let attempts = 0;
		let cancelled = false;
		const scheduleRetry = () => {
			if (cancelled) return;
			const delay = Math.min(10000, 1500 * Math.pow(1.5, attempts++));
			timer = window.setTimeout(load, delay);
		};
		const load = () =>
			adminFetch(
				`/api/admin/library-items/${entityId}/image?imageType=${imageType}&locale=${encodeURIComponent(locale)}`,
				session,
				{ signal: controller.signal, cache: "no-store" },
			)
				.then(async (response) => {
					if (response.status === 202) {
						setState("loading");
						scheduleRetry();
						return;
					}
					if (!response.ok) {
						setState("missing");
						return;
					}
					const nextUrl = URL.createObjectURL(await response.blob());
					if (cancelled) {
						URL.revokeObjectURL(nextUrl);
						return;
					}
					if (objectUrl) URL.revokeObjectURL(objectUrl);
					objectUrl = nextUrl;
					setUrl(objectUrl);
					setState("ready");
				})
				.catch((caught) => {
					if ((caught as Error).name !== "AbortError") setState("missing");
				});
		load();
		return () => {
			cancelled = true;
			controller.abort();
			if (timer) window.clearTimeout(timer);
			if (objectUrl) URL.revokeObjectURL(objectUrl);
		};
	}, [entityId, imageType, locale, revision, session]);
	return (
		<div
			className={`flex ${landscape ? "aspect-video" : "aspect-[2/3]"} items-center justify-center overflow-hidden bg-[#0d0d0e]`}
		>
			{url && state === "ready" ? (
				<img
					src={url}
					alt={alt}
					loading="lazy"
					className={`h-full w-full ${imageType === "Logo" ? "object-contain p-4" : "object-cover"} transition duration-300 group-hover:scale-105`}
				/>
			) : (
				<IconPhoto
					className={state === "missing" ? "material-muted" : "text-[#5ee3d8]"}
					size={28}
				/>
			)}
		</div>
	);
}

function EntityDetailView({
	entry,
	session,
	locale,
	onBack,
	onOpen,
}: {
	entry: NavigationEntry;
	session: Session;
	locale: string;
	onBack: () => void;
	onOpen: (item: Navigable) => void;
}) {
	const [detail, setDetail] = useState<Item | null>(null);
	const [error, setError] = useState("");
	const [refreshingMetadata, setRefreshingMetadata] = useState(false);
	const [refreshMessage, setRefreshMessage] = useState("");
	const [retry, setRetry] = useState(0);
	useEffect(() => {
		setRefreshMessage("");
	}, [entry.id, locale]);
	useEffect(() => {
		const controller = new AbortController();
		let cancelled = false;
		const load = () =>
			adminFetch(
				`/api/admin/library-items/${entry.id}?locale=${encodeURIComponent(locale)}`,
				session,
				{ signal: controller.signal },
			)
				.then(async (response) => {
					if (!response.ok) throw new Error("The item could not be loaded.");
					const value = (await response.json()) as Item;
					if (cancelled) return;
					setDetail(value);
				})
				.catch((caught) => {
					if (!cancelled && (caught as Error).name !== "AbortError")
						setError(
							caught instanceof Error
								? caught.message
								: "The item could not be loaded.",
						);
				});
		setDetail(null);
		setError("");
		load();
		return () => {
			cancelled = true;
			controller.abort();
		};
	}, [entry.id, locale, retry, session]);
	const children = detail?.children || [];
	const metadataPending = !detail?.metadata;
	const childLabel =
		detail?.type === "series"
			? "Seasons"
			: detail?.type === "season"
				? "Episodes"
				: detail?.type === "artist"
					? "Releases"
					: detail?.type === "release"
						? "Tracks"
						: detail?.type === "collection"
							? "Items"
							: "";
	const episodeView = detail?.type === "season";
	const hasChildren = children.length > 0;
	const canRefreshMetadata =
		detail && ["movie", "series", "season", "episode"].includes(detail.type);
	const refreshMetadata = async () => {
		if (!detail || refreshingMetadata) return;
		setRefreshingMetadata(true);
		setRefreshMessage("");
		setError("");
		try {
			const response = await adminFetch(
				`/api/admin/library-items/${detail.id}/metadata/refresh`,
				session,
				{ method: "POST" },
			);
			const payload = (await response.json().catch(() => null)) as {
				state?: string;
				failures?: { provider?: string }[];
				detail?: string | { message?: string };
			} | null;
			if (!response.ok) {
				const message =
					typeof payload?.detail === "string"
						? payload.detail
						: payload?.detail?.message;
				throw new Error(message || "Metadata and artwork could not be refreshed.");
			}
			setRefreshMessage(
				payload?.state === "completed_with_warnings"
					? `Metadata and artwork refreshed with ${payload.failures?.length || 0} provider warning(s).`
					: "Metadata and artwork refreshed.",
			);
			setDetail(null);
			setRetry((value) => value + 1);
		} catch (caught) {
			setError(
				caught instanceof Error
					? caught.message
					: "Metadata and artwork could not be refreshed.",
			);
		} finally {
			setRefreshingMetadata(false);
		}
	};
	return (
		<div className="dashboard-page min-h-[calc(100vh-7rem)]">
			<button onClick={onBack} className="material-back">
				<IconArrowLeft size={17} />
				Back
			</button>
			<div className="dashboard-page-header mt-6 flex items-start justify-between gap-4">
				<div>
					<p className="text-xs uppercase tracking-[.18em] material-muted">
						{detail?.type || entry.type}
					</p>
					{metadataPending ? (
						<div
							className="mt-3 h-8 w-64 max-w-[60vw] animate-pulse rounded bg-white/10"
							aria-hidden="true"
						/>
					) : (
						<h1 className="mt-2 text-3xl font-semibold tracking-tight">
							{detail?.metadata?.title}
						</h1>
					)}
				</div>
				<div className="flex items-center gap-2">
					{canRefreshMetadata && (
						<button
							type="button"
							onClick={refreshMetadata}
							disabled={refreshingMetadata}
							className="flex items-center gap-2 rounded-xl border console-divider px-4 py-2.5 text-sm font-semibold disabled:cursor-wait disabled:opacity-60"
						>
							<IconRefresh
								size={16}
								className={refreshingMetadata ? "animate-spin" : ""}
							/>
							{refreshingMetadata ? "Refreshing…" : "Refresh metadata & artwork"}
						</button>
					)}
					<button
						onClick={onBack}
						className="material-icon-button"
						aria-label="Close detail"
					>
						<IconX size={17} />
					</button>
				</div>
			</div>
			{refreshMessage && (
				<div className="dashboard-alert material-alert mt-5" role="status">
					<IconRefresh size={18} />
					{refreshMessage}
				</div>
			)}
			{error && (
				<div className="dashboard-alert material-alert mt-5" role="alert">
					<IconAlertCircle size={18} />
					{error}
					<button
						onClick={() => setRetry((value) => value + 1)}
						className="material-text-button ml-auto"
					>
						Retry
					</button>
				</div>
			)}
			{detail && (
				<div
					className={
						hasChildren
							? "mt-7 grid items-start gap-8 lg:grid-cols-[240px_minmax(0,1fr)]"
							: "mt-7 space-y-6"
					}
				>
					<div className={`space-y-5 ${hasChildren ? "" : "max-w-[240px]"}`}>
						{!metadataPending && (
							<ArtworkGallery
								entityId={entry.id}
								session={session}
								locale={locale}
								title={detail.metadata?.title || ""}
							/>
						)}
						{hasChildren && !metadataPending && <MetadataSummary detail={detail} />}
						{hasChildren && detail.trickplay && (
							<TrickplayAssetPanel
								entityId={detail.id}
								asset={detail.trickplay}
								session={session}
							/>
						)}
					</div>
					<div className="min-w-0">
						{hasChildren ? (
							<>
								{childLabel && (
									<div className="mb-5">
										<p className="text-xs uppercase tracking-[.18em] material-muted">
											{childLabel}
										</p>
										<p className="mt-1 text-sm material-muted">
											Choose an item to continue
										</p>
									</div>
								)}
								<div
									className={
										episodeView ? "grid gap-3 sm:grid-cols-2" : "grid justify-start gap-4"
									}
									style={
										episodeView
											? undefined
											: {
													gridTemplateColumns: "repeat(auto-fill, minmax(180px, 220px))",
												}
									}
								>
									{children.map((child) =>
										episodeView ? (
											<EpisodeCard
												key={child.id}
												item={child}
												session={session}
												locale={locale}
												onOpen={() => onOpen(child)}
											/>
										) : (
											<EntityCard
												key={child.id}
												item={child}
												session={session}
												locale={locale}
												onOpen={() => onOpen(child)}
											/>
										),
									)}
								</div>
							</>
						) : (
							<div className="space-y-5">
								<section className="dashboard-card p-6">
									{!metadataPending && (
										<div className="max-w-4xl text-sm leading-6">
											<MetadataSummary detail={detail} />
										</div>
									)}
								</section>
								{detail.trickplay && (
									<TrickplayAssetPanel
										entityId={detail.id}
										asset={detail.trickplay}
										session={session}
									/>
								)}
								{detail.type === "episode" && (
									<IntroOutroInspectionPanel entityId={detail.id} session={session} />
								)}
							</div>
						)}
					</div>
				</div>
			)}
		</div>
	);
}

function IntroOutroInspectionPanel({
	entityId,
	session,
}: {
	entityId: string;
	session: Session;
}) {
	const [inspection, setInspection] = useState<IntroOutroInspection | null>(
		null,
	);
	const [error, setError] = useState("");
	const [audioUrl, setAudioUrl] = useState("");
	const [audioKind, setAudioKind] = useState<"intro" | "outro" | null>(null);
	const [loadingAudio, setLoadingAudio] = useState<"intro" | "outro" | null>(
		null,
	);
	const audioUrlRef = useRef("");
	useEffect(() => {
		const controller = new AbortController();
		setInspection(null);
		setError("");
		adminFetch(`/api/admin/library-items/${entityId}/intro-outro`, session, {
			signal: controller.signal,
		})
			.then(async (response) => {
				if (!response.ok)
					throw new Error("Intro and outro inspection could not be loaded.");
				setInspection((await response.json()) as IntroOutroInspection);
			})
			.catch((caught) => {
				if ((caught as Error).name !== "AbortError")
					setError(
						caught instanceof Error
							? caught.message
							: "Intro and outro inspection could not be loaded.",
					);
			});
		return () => controller.abort();
	}, [entityId, session]);
	useEffect(
		() => () => {
			if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
		},
		[],
	);
	const loadAudio = async (kind: "intro" | "outro") => {
		setLoadingAudio(kind);
		setError("");
		try {
			const response = await adminFetch(
				`/api/admin/library-items/${entityId}/intro-outro/${kind}.mp3`,
				session,
			);
			if (!response.ok)
				throw new Error("The detected audio clip could not be loaded.");
			const nextUrl = URL.createObjectURL(await response.blob());
			if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
			audioUrlRef.current = nextUrl;
			setAudioUrl(nextUrl);
			setAudioKind(kind);
		} catch (caught) {
			setError(
				caught instanceof Error
					? caught.message
					: "The detected audio clip could not be loaded.",
			);
		} finally {
			setLoadingAudio(null);
		}
	};
	const partialWarning =
		inspection?.state === "scanned" && Boolean(inspection.error);
	const state =
		inspection?.state === "scanned"
			? partialWarning
				? "Ready with warnings"
				: "Ready"
			: inspection?.state === "failed"
				? "Failed"
				: "Pending";
	const stateClass = partialWarning
		? "border-[#f0bf6a]/30 text-[#f0bf6a]"
		: "border-[#5ee3d8]/30 text-[#5ee3d8]";
	return (
		<section className="dashboard-card space-y-4 p-5">
			<div className="flex flex-wrap items-center justify-between gap-3">
				<div>
					<p className="text-sm material-muted">
						Detected ranges and a downsampled Chromaprint bit-density preview. This is
						not an audio waveform.
					</p>
				</div>
				<span
					className={`rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-wide ${stateClass}`}
				>
					{state}
				</span>
			</div>
			{error && <p className="material-alert text-sm">{error}</p>}
			{partialWarning && inspection?.error && (
				<p className="rounded-lg border border-[#f0bf6a]/20 bg-[#f0bf6a]/5 px-3 py-2 text-sm text-[#f0bf6a]">
					One fingerprint window could not be read; available fingerprint data and
					matches are still shown. {inspection.error}
				</p>
			)}
			{inspection ? (
				<div className="grid gap-4 lg:grid-cols-2">
					{(["intro", "outro"] as const).map((kind) => {
						const fingerprint = inspection.fingerprints.find(
							(item) => item.type === kind,
						);
						const segment = inspection.segments.find((item) => item.type === kind);
						return (
							<FingerprintPreview
								key={kind}
								kind={kind}
								fingerprint={fingerprint}
								segment={segment}
								durationSeconds={inspection.durationSeconds || 0}
								loading={loadingAudio === kind}
								onListen={() => void loadAudio(kind)}
							/>
						);
					})}
				</div>
			) : !error ? (
				<p className="text-sm material-muted">Loading inspection…</p>
			) : null}
			{audioUrl && audioKind && (
				<div className="rounded-lg border border-white/10 bg-black/20 p-3">
					<p className="mb-2 text-xs font-medium capitalize text-white/80">
						{audioKind} audio preview (first 30 seconds)
					</p>
					<audio controls autoPlay src={audioUrl} className="w-full" />
				</div>
			)}
		</section>
	);
}

function FingerprintPreview({
	kind,
	fingerprint,
	segment,
	durationSeconds,
	loading,
	onListen,
}: {
	kind: "intro" | "outro";
	fingerprint?: IntroOutroInspection["fingerprints"][number];
	segment?: IntroOutroInspection["segments"][number];
	durationSeconds: number;
	loading: boolean;
	onListen: () => void;
}) {
	const values = fingerprint?.values || [];
	const points = values
		.map(
			(value, index) =>
				`${(index / Math.max(1, values.length - 1)) * 320},${64 - (value / 32) * 56 - 4}`,
		)
		.join(" ");
	const start = segment?.startSeconds ?? fingerprint?.startSeconds ?? 0;
	const end = segment?.endSeconds ?? fingerprint?.endSeconds ?? 0;
	const left =
		durationSeconds > 0 ? Math.min(100, (start / durationSeconds) * 100) : 0;
	const width =
		durationSeconds > 0
			? Math.max(1, Math.min(100 - left, ((end - start) / durationSeconds) * 100))
			: 0;
	return (
		<div className="rounded-xl border border-white/10 bg-black/15 p-4">
			<div className="flex items-center justify-between gap-3">
				<p className="text-sm font-semibold capitalize">{kind}</p>
				{segment ? (
					<button
						onClick={onListen}
						disabled={loading}
						className="material-text-button inline-flex items-center gap-1 disabled:opacity-50"
					>
						<IconPlayerPlay size={14} />
						{loading ? "Preparing…" : "Listen"}
					</button>
				) : null}
			</div>
			<p className="mt-1 text-xs material-muted">
				{fingerprint?.pointCount
					? `${fingerprint.pointCount.toLocaleString()} fingerprint points`
					: "No fingerprint data yet"}
			</p>
			{values.length > 1 ? (
				<svg
					viewBox="0 0 320 64"
					role="img"
					aria-label={`${kind} Chromaprint bit-density preview`}
					className="mt-3 h-20 w-full rounded bg-black/35"
				>
					<polyline fill="none" stroke="#5ee3d8" strokeWidth="2" points={points} />
				</svg>
			) : (
				<div className="mt-3 flex h-20 items-center justify-center rounded bg-black/35 text-xs material-muted">
					Waiting for audio fingerprinting
				</div>
			)}
			<div className="mt-3">
				<div className="h-1.5 overflow-hidden rounded-full bg-white/10">
					<div
						className={
							segment
								? "h-full rounded-full bg-[#5ee3d8]"
								: "h-full rounded-full bg-white/20"
						}
						style={{ marginLeft: `${left}%`, width: `${width}%` }}
					/>
				</div>
				<p className="mt-2 text-xs material-muted">
					{segment
						? `Detected ${formatDuration(segment.startSeconds)}–${formatDuration(segment.endSeconds)}`
						: `Fingerprint window ${formatDuration(start)}–${formatDuration(end)}`}
				</p>
			</div>
		</div>
	);
}

function formatDuration(value: number) {
	const seconds = Math.max(0, Math.floor(value || 0));
	return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function TrickplayAssetPanel({
	entityId,
	asset,
	session,
}: {
	entityId: string;
	asset: TrickplayAsset;
	session: Session;
}) {
	const [selectedSheet, setSelectedSheet] = useState(0);
	const [url, setUrl] = useState("");
	const [loadError, setLoadError] = useState("");
	const sheet =
		asset.sheets.find((entry) => entry.index === selectedSheet) ||
		asset.sheets[0];
	useEffect(() => {
		if (asset.state !== "ready" || !asset.generation || !sheet) {
			setUrl("");
			return;
		}
		const controller = new AbortController();
		let objectUrl = "";
		setLoadError("");
		adminFetch(
			`/api/admin/library-items/${entityId}/trickplay/${asset.generation}/${sheet.index}.webp`,
			session,
			{ signal: controller.signal },
		)
			.then(async (response) => {
				if (!response.ok)
					throw new Error("The trickplay sheet could not be loaded.");
				objectUrl = URL.createObjectURL(await response.blob());
				setUrl(objectUrl);
			})
			.catch((error) => {
				if ((error as Error).name !== "AbortError")
					setLoadError("The trickplay sheet could not be loaded.");
			});
		return () => {
			controller.abort();
			if (objectUrl) URL.revokeObjectURL(objectUrl);
		};
	}, [asset.generation, asset.state, entityId, session, sheet]);
	const status =
		asset.state === "ready"
			? "Ready"
			: asset.state === "failed"
				? "Failed"
				: "Pending";
	return (
		<section className="space-y-3">
			<div className="flex items-center justify-between gap-3">
				<p className="text-xs uppercase tracking-[.18em] material-muted">
					Trickplay assets
				</p>
				<span className="rounded-full border border-[#5ee3d8]/30 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-[#5ee3d8]">
					{status}
				</span>
			</div>
			<div className="material-surface overflow-hidden rounded-xl border border-white/10">
				<div className="grid grid-cols-3 gap-3 border-b border-white/10 px-3 py-3 text-xs material-muted">
					<span>
						{asset.frameWidth} × {asset.frameHeight} px
					</span>
					<span>{asset.intervalSeconds}s interval</span>
					<span>{asset.frameCount} frames</span>
				</div>
				{asset.state === "ready" && sheet ? (
					<div className="space-y-3 p-3">
						{asset.sheets.length > 1 && (
							<label className="block text-xs material-muted">
								Sprite sheet
								<select
									value={sheet.index}
									onChange={(event) => setSelectedSheet(Number(event.target.value))}
									className="console-input mt-1 h-9 w-full rounded-lg px-2 text-sm"
								>
									{asset.sheets.map((entry) => (
										<option key={entry.index} value={entry.index}>
											Sheet {entry.index + 1} ({entry.frameCount} frames)
										</option>
									))}
								</select>
							</label>
						)}
						{url ? (
							<img
								src={url}
								alt={`Trickplay sprite sheet ${sheet.index + 1}`}
								className="max-h-[360px] w-full rounded-lg bg-black object-contain"
							/>
						) : (
							<div className="flex aspect-video items-center justify-center rounded-lg bg-black text-xs material-muted">
								{loadError || "Loading sprite sheet…"}
							</div>
						)}
					</div>
				) : (
					<p className="px-3 py-4 text-xs material-muted">
						{asset.error ||
							"No ready sprite sheets yet. The scheduled trickplay task will generate them."}
					</p>
				)}
			</div>
		</section>
	);
}

function ArtworkGallery({
	entityId,
	session,
	locale,
	title,
}: {
	entityId: string;
	session: Session;
	locale: string;
	title: string;
}) {
	return (
		<div className="space-y-3">
			<p className="text-xs uppercase tracking-[.18em] material-muted">Artwork</p>
			<div className="grid grid-cols-2 gap-3">
				{artworkTypes.map((imageType) => (
					<div
						key={imageType}
						className="overflow-hidden rounded-xl border border-white/10 bg-[#0d0d0e]"
					>
						<EntityPoster
							entityId={entityId}
							session={session}
							locale={locale}
							imageType={imageType}
							landscape={imageType !== "Primary"}
							alt={`${imageType} artwork for ${title}`}
						/>
						<p className="border-t border-white/10 px-3 py-2 text-[11px] material-muted">
							{imageType}
						</p>
					</div>
				))}
			</div>
		</div>
	);
}

function MetadataSummary({ detail }: { detail: Item }) {
	const metadata = detail.metadata || {};
	const list = (value: unknown) =>
		Array.isArray(value)
			? value
					.filter(
						(entry): entry is string | number =>
							typeof entry === "string" || typeof entry === "number",
					)
					.join(", ") || "—"
			: typeof value === "string" || typeof value === "number"
				? value
				: "—";
	return (
		<div className="space-y-2 text-xs">
			<p className="leading-5">
				{metadata.description ||
					metadata.overview ||
					"No description cached for this language."}
			</p>
			<p>
				<span className="material-muted">Status:</span> {metadata.status || "—"} ·{" "}
				<span className="material-muted">Date:</span>{" "}
				{metadata.date || metadata.releaseDate || "—"}
			</p>
			<p>
				<span className="material-muted">Runtime:</span>{" "}
				{metadata.runtimeMinutes ? `${metadata.runtimeMinutes} min` : "—"}
			</p>
			<p>
				<span className="material-muted">Genres:</span>{" "}
				{list(metadata.tags || metadata.genres)}
			</p>
			<p>
				<span className="material-muted">Studios:</span> {list(metadata.studios)}
			</p>
			{metadata.trailers?.length ? (
				<p>
					<span className="material-muted">Trailers:</span>{" "}
					{metadata.trailers.map((trailer, index) => (
						<a
							key={`${trailer.url || trailer.key || "trailer"}-${index}`}
							href={trailer.url}
							target="_blank"
							rel="noreferrer"
							className="mr-2 text-[#5ee3d8] hover:underline"
						>
							{trailer.name || trailer.language || `Trailer ${index + 1}`}
						</a>
					))}
				</p>
			) : null}
			<p>
				<span className="material-muted">Provider IDs:</span>{" "}
				{detail.providerIds
					.map(
						(value) =>
							`${value.provider}:${value.id}${value.primary ? " (primary)" : ""}`,
					)
					.join(" · ") || "—"}
			</p>
		</div>
	);
}

export default function LibraryPreviewPage() {
	return (
		<Suspense
			fallback={
				<div className="material-surface rounded-xl p-8 text-sm material-muted">
					Loading view…
				</div>
			}
		>
			<LibraryViewPage />
		</Suspense>
	);
}
