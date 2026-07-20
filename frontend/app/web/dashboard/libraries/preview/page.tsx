"use client";

import Link from "next/link";
import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
	IconAlertCircle,
	IconArrowLeft,
	IconChevronLeft,
	IconChevronRight,
	IconX,
	IconPhoto,
	IconRefresh,
} from "@tabler/icons-react";
import {
	adminFetch,
	readSession,
	Session,
} from "../../components/admin-client";

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
	providerIds: { provider: string; id: string }[];
	children?: { id: string; type: string; relativePath?: string; seasonNumber?: number; episodeNumber?: number; trackNumber?: number }[];
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
const pageSize = 30;

function LibraryViewPage() {
	const params = useSearchParams();
	const libraryId = params.get("libraryId") || "";
	const [session, setSession] = useState<Session | null>(null);
	const [items, setItems] = useState<Item[]>([]);
	const [libraryName, setLibraryName] = useState("");
	const [parent, setParent] = useState<string | null>(null);
	const [locale, setLocale] = useState("en");
	const [page, setPage] = useState(1);
	const [total, setTotal] = useState(0);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [selectedId, setSelectedId] = useState<string | null>(null);
	const requestId = useRef(0);
	const abortRef = useRef<AbortController | null>(null);

	async function load(
		current = session,
		parentId = parent,
		currentPage = page,
	) {
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
					"/api/admin/libraries/" +
						libraryId +
						"/items?parentId=" +
						encodeURIComponent(parentId || "") +
						"&locale=" +
						encodeURIComponent(locale) +
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
			setItems(value.items || []);
			setTotal(value.total || 0);
		} catch (caught) {
			if ((caught as Error).name !== "AbortError" && id === requestId.current)
				setError(
					caught instanceof Error
						? caught.message
						: "The library could not be loaded.",
				);
		} finally {
			if (id === requestId.current) setLoading(false);
		}
	}

	useEffect(() => {
		const current = readSession();
		setSession(current);
		setPage(1);
		setParent(null);
		setSelectedId(null);
		if (current) load(current, null, 1);
		return () => abortRef.current?.abort();
	}, [libraryId, locale]);

	useEffect(() => {
		if (session && page !== 1) load(session, parent, page);
	}, [page]);

	const hydrationSignature = items.map((item) => item.id + ":" + item.metadataState).join(",");
	useEffect(() => {
		if (!session || !items.some((item) => item.metadataState === "queued" || item.metadataState === "running")) return;
		const timer = window.setInterval(() => load(session, parent, page), 3000);
		return () => window.clearInterval(timer);
	}, [session, parent, page, locale, hydrationSignature]);

	function back() {
		setParent(null);
		setPage(1);
		load(session, null, 1);
	}
	const pageCount = Math.max(1, Math.ceil(total / pageSize));

	if (!libraryId)
		return (
			<div className="material-surface rounded-xl p-8 text-sm material-muted">
				Choose a library from the Libraries tab.
			</div>
		);
	return (
		<div className="max-w-6xl">
			<div className="material-topbar">
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
							onClick={() => load()}
							className="material-icon-button"
							aria-label="Refresh view"
							title="Refresh view"
						>
							<IconRefresh size={17} />
						</button>
					</div>
					<p className="mt-2 text-sm material-muted">
						{total.toLocaleString()} indexed entries
					</p>
				</div>
				<div className="flex items-center gap-2">
					<select
						value={locale}
						onChange={(event) => {
							setLocale(event.target.value);
							setItems([]);
						}}
						className="material-input h-10 rounded-lg px-3 text-sm outline-none"
					>
						<option value="en">English</option>
						<option value="ja">日本語</option>
					</select>
				</div>
			</div>
			{parent && (
				<button onClick={back} className="material-back mt-5">
					<IconArrowLeft size={15} />
					Back to library
				</button>
			)}
			{error && (
				<div className="material-alert mt-6">
					<IconAlertCircle size={18} />
					<span className="flex-1">{error}</span>
					<button onClick={() => load()} className="material-text-button">
						Retry
					</button>
				</div>
			)}
			{loading && !items.length && !error && (
				<div className="material-surface mt-7 rounded-xl p-8 text-sm material-muted">
					Loading entries…
				</div>
			)}
			{!loading && !error && (
				<>
					<div className="mt-7 flex flex-col gap-5 lg:flex-row">
					<div className="min-w-0 flex-1 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
						{items.map((item) => (
							<ViewCard
								key={item.id}
								item={item}
								session={session}
								locale={locale}
								onOpen={() => setSelectedId(item.id)}
							/>
						))}
						{!items.length && (
							<div className="material-surface col-span-full rounded-xl p-8 text-sm material-muted">
								No entries on this page.
							</div>
						)}
					</div>
						{selectedId && session && (
							<DetailPanel
								entityId={selectedId}
								session={session}
								locale={locale}
								onClose={() => setSelectedId(null)}
								onSelectChild={setSelectedId}
							/>
						)}
					</div>
					<div className="mt-6 flex items-center justify-between border-t material-divider pt-4">
						<span className="text-xs material-muted">
							Page {page} of {pageCount}
						</span>
						<div className="flex gap-2">
							<button
								disabled={page <= 1}
								onClick={() => setPage((value) => value - 1)}
								className="material-icon-button disabled:opacity-30"
								aria-label="Previous page"
							>
								<IconChevronLeft size={16} />
							</button>
							<button
								disabled={page >= pageCount}
								onClick={() => setPage((value) => value + 1)}
								className="material-icon-button disabled:opacity-30"
								aria-label="Next page"
							>
								<IconChevronRight size={16} />
							</button>
						</div>
					</div>
				</>
			)}
		</div>
	);
}

function ViewCard({
	item,
	session,
	locale,
	onOpen,
}: {
	item: Item;
	session: Session | null;
	locale: string;
	onOpen: () => void;
}) {
	const cardRef = useRef<HTMLButtonElement | null>(null);
	const [image, setImage] = useState<string | null>(null);
	const [imageFailed, setImageFailed] = useState(false);
	const [imagePending, setImagePending] = useState(false);
	useEffect(() => {
		const controller = new AbortController();
		let url = "";
		let retryTimer: number | undefined;
		let attempts = 0;
		setImage(null);
		setImageFailed(false);
		setImagePending(false);
		let inFlight = false;
		const fetchImage = () => {
			if (inFlight || !session) return;
			inFlight = true;
			adminFetch(
				"/api/admin/library-items/" +
					item.id +
					"/image?imageType=Primary&locale=" +
					encodeURIComponent(locale),
				session,
				{ signal: controller.signal },
			)
				.then(async (response) => {
					if (response.status === 202) {
						setImagePending(true);
						inFlight = false;
						if (++attempts < 20) retryTimer = window.setTimeout(fetchImage, 2000);
						else setImageFailed(true);
						return;
					}
					if (!response.ok) throw new Error("image");
					setImagePending(false);
					url = URL.createObjectURL(await response.blob());
					setImage(url);
				})
				.catch((caught) => {
					inFlight = false;
					if ((caught as Error).name !== "AbortError") setImageFailed(true);
				});
		};
		const node = cardRef.current;
		if (!node || typeof IntersectionObserver === "undefined") fetchImage();
		else {
			const observer = new IntersectionObserver(
				(entries) => {
					if (entries.some((entry) => entry.isIntersecting)) {
						fetchImage();
						observer.disconnect();
					}
				},
				{ rootMargin: "320px" },
			);
			observer.observe(node);
			return () => {
				observer.disconnect();
				controller.abort();
				if (retryTimer) window.clearTimeout(retryTimer);
				if (url) URL.revokeObjectURL(url);
			};
		}
		return () => {
			controller.abort();
			if (retryTimer) window.clearTimeout(retryTimer);
			if (url) URL.revokeObjectURL(url);
		};
	}, [item.id, session, locale]);
	const pending = item.metadataState === "queued" || item.metadataState === "running" || imagePending;
	return (
		<button
			ref={cardRef}
			onClick={onOpen}
			className="material-surface group overflow-hidden rounded-xl text-left transition hover:bg-[#282828]"
		>
			<div className="flex aspect-[2/3] items-center justify-center bg-[#0d0e13]">
				{image ? (
					<img
						src={image}
						alt=""
						loading="lazy"
						className="h-full w-full object-cover transition group-hover:scale-105"
					/>
				) : (
					<IconPhoto
						className={imageFailed ? "material-muted" : "text-[#b9c3ff]"}
						size={25}
					/>
				)}
			</div>
			<div className="p-3">
				<p className="truncate text-sm font-medium">
					{item.metadata?.title || item.displayName}
				</p>
				<p className="mt-1 truncate text-[11px] material-muted">
					{pending
						? "Loading metadata…"
						: item.metadataState === "error"
							? item.metadataError || "Metadata unavailable; retry hydration"
						: item.type + " · " + item.matchStatus}
				</p>
			</div>
		</button>
	);
}

function DetailPanel({
	entityId,
	session,
	locale,
	onClose,
	onSelectChild,
}: {
	entityId: string;
	session: Session;
	locale: string;
	onClose: () => void;
	onSelectChild: (id: string) => void;
}) {
	const [detail, setDetail] = useState<Item | null>(null);
	const [error, setError] = useState("");
	const [timedOut, setTimedOut] = useState(false);
	const [retry, setRetry] = useState(0);
	useEffect(() => {
		const controller = new AbortController();
		let timer: number | undefined;
		let polls = 0;
		let cancelled = false;
		setDetail(null);
		setError("");
		setTimedOut(false);
		async function load() {
			try {
				const response = await adminFetch(
					"/api/admin/library-items/" + entityId + "?locale=" + encodeURIComponent(locale),
					session,
					{ signal: controller.signal },
				);
				if (!response.ok) throw new Error("The item could not be loaded.");
				const value = (await response.json()) as Item;
				if (cancelled) return;
				setDetail(value);
				const state = value.hydration?.state || value.metadataState;
				if ((state === "queued" || state === "running") && ++polls < 40) {
					timer = window.setTimeout(load, 1500);
				} else if (state === "queued" || state === "running") {
					setTimedOut(true);
				}
			} catch (caught) {
				if (!cancelled && (caught as Error).name !== "AbortError") setError(caught instanceof Error ? caught.message : "The item could not be loaded.");
			}
		}
		load();
		return () => {
			cancelled = true;
			controller.abort();
			if (timer) window.clearTimeout(timer);
		};
	}, [entityId, locale, retry, session]);

	async function requestRetry() {
		await adminFetch("/api/admin/library-items/hydrate", session, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ entityIds: [entityId], locale }),
		});
		setRetry((value) => value + 1);
	}

	const metadata: Metadata = detail?.metadata || {};
	const list = (value: unknown) => Array.isArray(value) ? value.filter((entry): entry is string | number => typeof entry === "string" || typeof entry === "number").join(", ") || "—" : typeof value === "string" || typeof value === "number" ? value : "—";
	const trailers = metadata.trailers || [];
	const people = metadata.people || [];
	const tracks = metadata.tracks || [];
	return (
		<aside className="material-surface w-full shrink-0 rounded-xl p-4 lg:w-[390px]">
			<div className="flex items-start justify-between gap-3">
				<div>
					<p className="text-xs uppercase tracking-wide material-muted">{detail?.type || "Item"}</p>
					<h2 className="mt-1 text-lg font-semibold">{metadata.title || detail?.displayName || "Loading…"}</h2>
				</div>
				<button onClick={onClose} className="material-icon-button" aria-label="Close details"><IconX size={17} /></button>
			</div>
			{error && <div className="material-alert mt-4 text-xs"><IconAlertCircle size={16} />{error}</div>}
			{detail && (detail.metadataState === "queued" || detail.metadataState === "running") && !metadata.title && (
				<div className="mt-4 rounded-lg bg-[#0d0e13] p-3 text-xs material-muted">Hydrating {locale} metadata…</div>
			)}
			{(detail?.metadataState === "error" || timedOut) && (
				<div className="mt-4 rounded-lg border border-red-900/60 bg-red-950/20 p-3 text-xs text-red-200">
					<p>{detail?.metadataError || (timedOut ? "Hydration is taking longer than expected." : "Metadata hydration failed.")}</p>
					<button onClick={requestRetry} className="material-text-button mt-2">Retry hydration</button>
				</div>
			)}
			<div className="mt-4 grid grid-cols-2 gap-2">
				{(["Primary", "Backdrop", "Logo", "Banner"] as const).map((type) => (
					<PanelImage key={type} entityId={entityId} type={type} locale={locale} session={session} />
				))}
			</div>
			<div className="mt-5 space-y-2 text-xs">
				<p className="font-medium">{metadata.description || metadata.overview || "No description cached for this language."}</p>
				<p><span className="material-muted">Provider IDs:</span> {detail?.providerIds.map((value) => `${value.provider}:${value.id}`).join(" · ") || "—"}</p>
				<p><span className="material-muted">Status:</span> {metadata.status || "—"} · <span className="material-muted">Date:</span> {metadata.date || metadata.releaseDate || "—"}</p>
				<p><span className="material-muted">Runtime:</span> {metadata.runtimeMinutes ? `${metadata.runtimeMinutes} min` : "—"} · <span className="material-muted">Air time:</span> {metadata.airTime || "—"}</p>
				<p><span className="material-muted">Genres:</span> {list(metadata.tags || metadata.genres)}</p>
				<p><span className="material-muted">Studios:</span> {list(metadata.studios)} · <span className="material-muted">Networks:</span> {list(metadata.networks)}</p>
				<p><span className="material-muted">Country/language:</span> {metadata.originalCountry || "—"} / {metadata.originalLanguage || "—"}</p>
				{metadata.albumArtist && <p><span className="material-muted">Album artist:</span> {metadata.albumArtist}</p>}
				{trailers.length > 0 && <p><span className="material-muted">Trailers:</span> {trailers.map((value) => value.name || value.key).filter(Boolean).join(", ")}</p>}
				{people.length > 0 && <p><span className="material-muted">Credits:</span> {people.map((value) => value.name).filter(Boolean).join(", ")}</p>}
				{tracks.length > 0 && <p><span className="material-muted">Tracks:</span> {tracks.map((value) => value.title).filter(Boolean).join(", ")}</p>}
			</div>
			{detail?.children?.length ? (
				<div className="mt-5 border-t material-divider pt-4">
					<p className="mb-2 text-xs font-medium">Children</p>
					<div className="space-y-1">{detail.children.map((child) => <button key={child.id} onClick={() => onSelectChild(child.id)} className="block w-full truncate rounded px-2 py-1 text-left text-xs hover:bg-white/10">{child.type} · {child.relativePath || child.id}</button>)}</div>
				</div>
			) : null}
		</aside>
	);
}

function PanelImage({ entityId, type, locale, session }: { entityId: string; type: "Primary" | "Backdrop" | "Logo" | "Banner"; locale: string; session: Session }) {
	const [url, setUrl] = useState<string | null>(null);
	useEffect(() => {
		const controller = new AbortController();
		setUrl(null);
		let objectUrl = "";
		let timer: number | undefined;
		let attempts = 0;
		const load = () => adminFetch(`/api/admin/library-items/${entityId}/image?imageType=${type}&locale=${encodeURIComponent(locale)}`, session, { signal: controller.signal })
			.then(async (response) => {
				if (response.status === 202 && ++attempts < 20) { timer = window.setTimeout(load, 2000); return; }
				if (!response.ok) return;
				objectUrl = URL.createObjectURL(await response.blob());
				setUrl(objectUrl);
			})
			.catch(() => undefined);
		load();
		return () => { controller.abort(); if (timer) window.clearTimeout(timer); if (objectUrl) URL.revokeObjectURL(objectUrl); };
	}, [entityId, locale, session, type]);
	return <div className="flex aspect-video items-center justify-center overflow-hidden rounded bg-[#0d0e13] text-[10px] material-muted">{url ? <img src={url} alt={type} className="h-full w-full object-cover" /> : type}</div>;
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
