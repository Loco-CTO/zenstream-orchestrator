"use client";

import Link from "next/link";
import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
	IconAlertCircle,
	IconArrowLeft,
	IconChevronLeft,
	IconChevronRight,
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
	metadata?: { title?: string } | null;
	matchStatus: string;
	providerIds: { provider: string; id: string }[];
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
		if (current) load(current, null, 1);
		return () => abortRef.current?.abort();
	}, [libraryId, locale]);

	useEffect(() => {
		if (session && page !== 1) load(session, parent, page);
	}, [page]);

	function openParent(id: string) {
		setParent(id);
		setPage(1);
		load(session, id, 1);
	}
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
					<div className="mt-7 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
						{items.map((item) => (
							<ViewCard
								key={item.id}
								item={item}
								session={session}
								locale={locale}
								onOpen={() => openParent(item.id)}
							/>
						))}
						{!items.length && (
							<div className="material-surface col-span-full rounded-xl p-8 text-sm material-muted">
								No entries on this page.
							</div>
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
	useEffect(() => {
		const controller = new AbortController();
		let url = "";
		setImage(null);
		setImageFailed(false);
		let started = false;
		const fetchImage = () => {
			if (started || !session) return;
			started = true;
			adminFetch(
				"/api/admin/library-items/" +
					item.id +
					"/image?imageType=poster&locale=" +
					encodeURIComponent(locale),
				session,
				{ signal: controller.signal },
			)
				.then(async (response) => {
					if (!response.ok) throw new Error("image");
					url = URL.createObjectURL(await response.blob());
					setImage(url);
				})
				.catch((caught) => {
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
				if (url) URL.revokeObjectURL(url);
			};
		}
		return () => {
			controller.abort();
			if (url) URL.revokeObjectURL(url);
		};
	}, [item.id, session, locale]);
	const pending = !item.metadata && item.providerIds.length > 0;
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
						? "Metadata pending scheduled refresh"
						: item.type + " · " + item.matchStatus}
				</p>
			</div>
		</button>
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
