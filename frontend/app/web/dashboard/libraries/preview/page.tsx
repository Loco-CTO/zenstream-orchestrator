"use client";

import Link from "next/link";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
	IconAlertCircle,
	IconArrowLeft,
	IconChevronLeft,
	IconChevronRight,
	IconPhoto,
	IconRefresh,
	IconX,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../../components/admin-client";

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
	metadataState?: "ready" | "queued" | "running" | "error";
	metadataError?: string | null;
	hydration?: { state?: string; error?: string | null; details?: string | null; attempts?: number };
	matchStatus: string;
	providerIds: { provider: string; id: string; primary?: boolean; role?: string }[];
	primaryProvider?: string | null;
	children?: Child[];
};

type MetadataPerson = { name?: string; role?: string };
type MetadataTrailer = { name?: string; key?: string };
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
type Navigable = { id: string; type: string; displayName?: string; relativePath?: string };
type CardItem = Navigable & { metadata?: Metadata | null; matchStatus?: string; seasonNumber?: number; episodeNumber?: number };
const pageSize = 30;

function LibraryViewPage() {
	const params = useSearchParams();
	const router = useRouter();
	const pathname = usePathname();
	const libraryId = params.get("libraryId") || "";
	const urlEntityId = params.get("entityId");
	const urlEntityPathValue = params.get("entityPath") || "";
	const [session, setSession] = useState<Session | null>(null);
	const [items, setItems] = useState<Item[]>([]);
	const [libraryName, setLibraryName] = useState("");
	const [parent, setParent] = useState<string | null>(null);
	const [locale, setLocale] = useState("en");
	const [page, setPage] = useState(1);
	const [total, setTotal] = useState(0);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [navigation, setNavigation] = useState<NavigationEntry[]>([]);
	const requestId = useRef(0);
	const abortRef = useRef<AbortController | null>(null);

	const load = useCallback(async (current: Session | null, parentId: string | null, currentPage: number) => {
		if (!current || !libraryId) return;
		const id = ++requestId.current;
		abortRef.current?.abort();
		const controller = new AbortController();
		abortRef.current = controller;
		setError("");
		if (!items.length) setLoading(true);
		try {
			const init = { signal: controller.signal };
			const [libraryResponse, itemsResponse] = await Promise.all([
				adminFetch("/api/admin/libraries/" + libraryId, current, init),
				adminFetch(
					"/api/admin/libraries/" + libraryId + "/items?parentId=" + encodeURIComponent(parentId || "") + "&locale=" + encodeURIComponent(locale) + "&page=" + currentPage + "&pageSize=" + pageSize,
					current,
					init,
				),
			]);
			if (id !== requestId.current) return;
			if (!libraryResponse.ok || !itemsResponse.ok) throw new Error("The library could not be loaded.");
			const [library, value] = await Promise.all([libraryResponse.json(), itemsResponse.json()]);
			if (id !== requestId.current) return;
			setLibraryName(library.name || "Library");
			setItems(value.items || []);
			setTotal(value.total || 0);
		} catch (caught) {
			if ((caught as Error).name !== "AbortError" && id === requestId.current) setError(caught instanceof Error ? caught.message : "The library could not be loaded.");
		} finally {
			if (id === requestId.current) setLoading(false);
		}
	}, [items.length, libraryId, locale]);

	useEffect(() => {
		const current = readSession();
		setSession(current);
		setPage(1);
		setParent(null);
		setNavigation([]);
		if (current) void load(current, null, 1);
		return () => abortRef.current?.abort();
	}, [libraryId, locale, load]);

	useEffect(() => {
		const ids = urlEntityPathValue.split(",").filter(Boolean);
		if (!urlEntityId && !ids.length) {
			setNavigation([]);
			return;
		}
		const path = ids.length ? ids : [urlEntityId as string];
		setNavigation((current) => current.length && current[current.length - 1].id === path[path.length - 1]
			? current
			: path.map((id) => ({ id, type: "item", label: "Library item" })));
	}, [urlEntityId, urlEntityPathValue]);

	useEffect(() => {
		if (session && page !== 1 && navigation.length === 0) void load(session, parent, page);
	}, [load, navigation.length, page, parent, session]);

	useEffect(() => {
		if (!session || navigation.length > 0 || !items.some((item) => item.metadataState === "queued" || item.metadataState === "running")) return;
		const timer = window.setInterval(() => void load(session, parent, page), 3000);
		return () => window.clearInterval(timer);
	}, [items, load, navigation.length, page, parent, session]);

	useEffect(() => {
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.key === "Escape" && navigation.length > 0) setNavigation((current) => current.slice(0, -1));
		};
		window.addEventListener("keydown", onKeyDown);
		return () => window.removeEventListener("keydown", onKeyDown);
	}, [navigation.length]);

	function openItem(item: Navigable) {
		const nextStack = [...navigation, { id: item.id, type: item.type, label: item.displayName || item.relativePath || item.type }];
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
	if (!libraryId) return <div className="material-surface rounded-xl p-8 text-sm material-muted">Choose a library from the Libraries tab.</div>;
	return (
		<div className="w-full max-w-none">
			{navigation.length === 0 ? (
				<>
					<div className="material-topbar">
						<div>
							<Link href="/web/dashboard/libraries" className="material-back"><IconArrowLeft size={15} />Libraries</Link>
							<div className="mt-4 flex items-center gap-3"><h1 className="text-3xl font-semibold tracking-tight">{libraryName || "Library"}</h1><button onClick={() => load(session, parent, page)} className="material-icon-button" aria-label="Refresh view" title="Refresh view"><IconRefresh size={17} /></button></div>
							<p className="mt-2 text-sm material-muted">{total.toLocaleString()} indexed entries</p>
						</div>
						<select value={locale} onChange={(event) => { setLocale(event.target.value); setItems([]); }} className="material-input h-10 rounded-lg px-3 text-sm outline-none"><option value="en">English</option><option value="ja">日本語</option></select>
					</div>
					{parent && <button onClick={() => { setParent(null); setPage(1); void load(session, null, 1); }} className="material-back mt-5"><IconArrowLeft size={15} />Back to library</button>}
					{error && <div className="material-alert mt-6"><IconAlertCircle size={18} /><span className="flex-1">{error}</span><button onClick={() => void load(session, parent, page)} className="material-text-button">Retry</button></div>}
					{loading && !items.length && !error && <div className="material-surface mt-7 rounded-xl p-8 text-sm material-muted">Loading entries…</div>}
					{!loading && !error && <>
						<div className="mt-7 grid w-full gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(150px, 220px))" }}>{items.map((item) => <EntityCard key={item.id} item={item} session={session} locale={locale} onOpen={() => openItem(item)} />)}{!items.length && <div className="material-surface col-span-full rounded-xl p-8 text-sm material-muted">No entries on this page.</div>}</div>
						<div className="mt-6 flex items-center justify-between border-t material-divider pt-4"><span className="text-xs material-muted">Page {page} of {pageCount}</span><div className="flex gap-2"><button disabled={page <= 1} onClick={() => setPage((value) => value - 1)} className="material-icon-button disabled:opacity-30" aria-label="Previous page"><IconChevronLeft size={16} /></button><button disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)} className="material-icon-button disabled:opacity-30" aria-label="Next page"><IconChevronRight size={16} /></button></div></div>
					</>}
				</>
			) : session ? <EntityDetailView entry={navigation[navigation.length - 1]} session={session} locale={locale} onBack={back} onOpen={openItem} /> : null}
		</div>
	);
}

function EntityCard({ item, session, locale, onOpen }: { item: CardItem; session: Session | null; locale: string; onOpen: () => void }) {
	const label = item.displayName || item.relativePath || item.type;
	return <button type="button" onClick={onOpen} className="material-surface group w-full max-w-[220px] overflow-hidden rounded-xl text-left transition hover:-translate-y-0.5 hover:bg-[#282828] hover:shadow-xl hover:shadow-black/30"><EntityPoster entityId={item.id} session={session} locale={locale} alt="" /><div className="p-3"><p className="truncate text-sm font-medium">{item.metadata?.title || label}</p><p className="mt-1 truncate text-[11px] material-muted">{item.type === "episode" && item.seasonNumber != null ? `S${item.seasonNumber}E${item.episodeNumber ?? "—"}` : item.type} · {item.matchStatus || "ready"}</p></div></button>;
}

function EpisodeCard({ item, session, locale, onOpen }: { item: Child; session: Session; locale: string; onOpen: () => void }) {
	const label = item.displayName || item.relativePath || `Episode ${item.episodeNumber ?? "—"}`;
	return <button type="button" onClick={onOpen} className="material-surface group flex min-w-0 w-full overflow-hidden rounded-xl text-left transition hover:-translate-y-0.5 hover:bg-[#282828] hover:shadow-xl hover:shadow-black/30"><EntityPoster entityId={item.id} session={session} locale={locale} alt="" landscape /><div className="min-w-0 flex-1 p-4"><p className="truncate text-sm font-semibold">{label}</p><p className="mt-2 text-xs material-muted">S{item.seasonNumber ?? "—"}E{item.episodeNumber ?? "—"}</p><p className="mt-1 text-[11px] material-muted">Open episode details</p></div></button>;
}

function EntityPoster({ entityId, session, locale, alt, landscape = false }: { entityId: string; session: Session | null; locale: string; alt: string; landscape?: boolean }) {
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
		const load = () => adminFetch(`/api/admin/library-items/${entityId}/image?imageType=Primary&locale=${encodeURIComponent(locale)}`, session, { signal: controller.signal }).then(async (response) => {
			if (response.status === 202 && attempts++ < 20) { timer = window.setTimeout(load, 1500); return; }
			if (!response.ok) { setState("missing"); return; }
			objectUrl = URL.createObjectURL(await response.blob());
			setUrl(objectUrl);
			setState("ready");
		}).catch((caught) => { if ((caught as Error).name !== "AbortError") setState("missing"); });
		load();
		return () => { controller.abort(); if (timer) window.clearTimeout(timer); if (objectUrl) URL.revokeObjectURL(objectUrl); };
	}, [entityId, locale, session]);
	return <div className={`flex ${landscape ? "aspect-video w-40 shrink-0" : "aspect-[2/3]"} items-center justify-center overflow-hidden bg-[#0d0e13]`}>{url && state === "ready" ? <img src={url} alt={alt} loading="lazy" className="h-full w-full object-cover transition duration-300 group-hover:scale-105" /> : <IconPhoto className={state === "missing" ? "material-muted" : "text-[#b9c3ff]"} size={28} />}</div>;
}

function EntityDetailView({ entry, session, locale, onBack, onOpen }: { entry: NavigationEntry; session: Session; locale: string; onBack: () => void; onOpen: (item: Navigable) => void }) {
	const [detail, setDetail] = useState<Item | null>(null);
	const [error, setError] = useState("");
	const [retry, setRetry] = useState(0);
	useEffect(() => {
		const controller = new AbortController();
		let timer: number | undefined;
		let cancelled = false;
		const load = () => adminFetch(`/api/admin/library-items/${entry.id}?locale=${encodeURIComponent(locale)}`, session, { signal: controller.signal }).then(async (response) => {
			if (!response.ok) throw new Error("The item could not be loaded.");
			const value = await response.json() as Item;
			if (cancelled) return;
			setDetail(value);
			const state = value.hydration?.state || value.metadataState;
			if ((state === "queued" || state === "running") && !cancelled) timer = window.setTimeout(load, 1500);
		}).catch((caught) => { if (!cancelled && (caught as Error).name !== "AbortError") setError(caught instanceof Error ? caught.message : "The item could not be loaded."); });
		setDetail(null); setError(""); load();
		return () => { cancelled = true; controller.abort(); if (timer) window.clearTimeout(timer); };
	}, [entry.id, locale, retry, session]);
	const children = detail?.children || [];
	const childLabel = detail?.type === "series" ? "Seasons" : detail?.type === "season" ? "Episodes" : detail?.type === "artist" ? "Releases" : detail?.type === "release" ? "Tracks" : detail?.type === "collection" ? "Items" : "";
	const episodeView = detail?.type === "season";
	return <div className="min-h-[calc(100vh-7rem)]"><button onClick={onBack} className="material-back"><IconArrowLeft size={17} />Back</button><div className="mt-6 flex items-start justify-between gap-4"><div><p className="text-xs uppercase tracking-[.18em] material-muted">{detail?.type || entry.type}</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">{detail?.metadata?.title || entry.label}</h1></div><button onClick={onBack} className="material-icon-button" aria-label="Close detail"><IconX size={17} /></button></div>{error && <div className="material-alert mt-5"><IconAlertCircle size={18} />{error}<button onClick={() => setRetry((value) => value + 1)} className="material-text-button ml-auto">Retry</button></div>}{detail && <div className="mt-7 grid items-start gap-8 lg:grid-cols-[240px_minmax(0,1fr)]"><div className="space-y-5"><div className="overflow-hidden rounded-2xl border border-white/10 bg-[#0d0e13]"><EntityPoster entityId={entry.id} session={session} locale={locale} alt={detail.metadata?.title || entry.label} /></div><MetadataSummary detail={detail} /></div><div className="min-w-0">{childLabel && <div className="mb-5"><p className="text-xs uppercase tracking-[.18em] material-muted">{childLabel}</p><p className="mt-1 text-sm material-muted">Choose an item to continue</p></div>}{children.length ? <div className={episodeView ? "grid gap-3 sm:grid-cols-2" : "grid justify-start gap-4"} style={episodeView ? undefined : { gridTemplateColumns: "repeat(auto-fill, minmax(180px, 220px))" }}>{children.map((child) => episodeView ? <EpisodeCard key={child.id} item={child} session={session} locale={locale} onOpen={() => onOpen(child)} /> : <EntityCard key={child.id} item={child} session={session} locale={locale} onOpen={() => onOpen(child)} />)}</div> : <div className="material-surface rounded-xl p-6 text-sm material-muted">No child entries available.</div>}</div></div>}</div>;
}

function MetadataSummary({ detail }: { detail: Item }) {
	const metadata = detail.metadata || {};
	const list = (value: unknown) => Array.isArray(value) ? value.filter((entry): entry is string | number => typeof entry === "string" || typeof entry === "number").join(", ") || "—" : typeof value === "string" || typeof value === "number" ? value : "—";
	return <div className="space-y-2 text-xs"><p className="leading-5">{metadata.description || metadata.overview || "No description cached for this language."}</p><p><span className="material-muted">Status:</span> {metadata.status || "—"} · <span className="material-muted">Date:</span> {metadata.date || metadata.releaseDate || "—"}</p><p><span className="material-muted">Runtime:</span> {metadata.runtimeMinutes ? `${metadata.runtimeMinutes} min` : "—"}</p><p><span className="material-muted">Genres:</span> {list(metadata.tags || metadata.genres)}</p><p><span className="material-muted">Studios:</span> {list(metadata.studios)}</p><p><span className="material-muted">Provider IDs:</span> {detail.providerIds.map((value) => `${value.provider}:${value.id}${value.primary ? " (primary)" : ""}`).join(" · ") || "—"}</p></div>;
}

export default function LibraryPreviewPage() {
	return <Suspense fallback={<div className="material-surface rounded-xl p-8 text-sm material-muted">Loading view…</div>}><LibraryViewPage /></Suspense>;
}
