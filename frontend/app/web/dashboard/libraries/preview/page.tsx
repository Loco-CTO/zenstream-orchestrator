"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { IconArrowLeft, IconChevronRight, IconPhoto, IconRefresh } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../../components/admin-client";

type Item = { id: string; type: string; displayName: string; relativePath?: string; parentId?: string | null; metadata?: { title?: string; overview?: string; images?: unknown[] } | null; matchStatus: string; providerIds: { provider: string; id: string }[] };

function PreviewContent() {
	const params = useSearchParams();
	const libraryId = params.get("libraryId") || "";
	const [session, setSession] = useState<Session | null>(null);
	const [items, setItems] = useState<Item[]>([]);
	const [libraryName, setLibraryName] = useState("");
	const [parent, setParent] = useState<string | null>(null);
	const [locale, setLocale] = useState("en");
	const [loading, setLoading] = useState(true);
	const [message, setMessage] = useState("");

	async function load(current = session, parentId = parent) {
		if (!current || !libraryId) return;
		setLoading(true);
		const [libraryResponse, itemsResponse] = await Promise.all([
			adminFetch(`/api/admin/libraries/${libraryId}`, current),
			adminFetch(`/api/admin/libraries/${libraryId}/items?parentId=${encodeURIComponent(parentId || "")}&locale=${encodeURIComponent(locale)}`, current),
		]);
		if (libraryResponse.ok) setLibraryName((await libraryResponse.json()).name);
		if (itemsResponse.ok) {
			const value = await itemsResponse.json();
			setItems(value.items || []);
			const missing = (value.items || []).filter((item: Item) => !item.metadata && item.providerIds.length).map((item: Item) => item.id);
			if (missing.length) {
				const hydrate = await adminFetch("/api/admin/library-items/hydrate", current, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entityIds: missing.slice(0, 20), locale }) });
				if (hydrate.ok) {
					const hydrated = await hydrate.json();
					const byId = new Map<string, Item["metadata"]>((hydrated.items || []).map((entry: { entityId: string; metadata: Item["metadata"] }) => [entry.entityId, entry.metadata]));
					setItems((currentItems) => currentItems.map((item) => byId.has(item.id) ? { ...item, metadata: byId.get(item.id) } : item));
				}
			}
		}
		setLoading(false);
		setMessage("");
	}
useEffect(() => { const current = readSession(); setSession(current); if (current) load(current, null); }, [libraryId, locale]);

	if (!libraryId) return <div className="console-card rounded-2xl p-8 text-sm console-muted">Choose a library from the Libraries tab.</div>;
	return <div><div className="flex flex-wrap items-center justify-between gap-4"><div><Link href="/web/dashboard/libraries" className="flex items-center gap-2 text-sm console-muted hover:text-white"><IconArrowLeft size={16} />Libraries</Link><p className="mt-5 console-kicker">Preview</p><h1 className="mt-2 text-4xl font-black tracking-tight">{libraryName || "Library"}</h1></div><div className="flex items-center gap-2"><select value={locale} onChange={(event) => { setLocale(event.target.value); setParent(null); }} className="console-input h-10 rounded-xl px-3 text-sm outline-none"><option value="en">English</option><option value="ja">日本語</option></select><button onClick={() => load()} className="rounded-xl border console-divider p-2.5 console-muted hover:bg-white/10" aria-label="Refresh"><IconRefresh size={17} /></button></div></div>{parent && <button onClick={() => { setParent(null); load(session, null); }} className="mt-6 flex items-center gap-2 text-sm text-[#8fe4cf]"><IconArrowLeft size={16} />Back to library</button>}{message && <p className="mt-4 text-sm text-[#8fe4cf]">{message}</p>}{loading ? <div className="mt-8 console-card rounded-2xl p-8 text-sm console-muted">Loading preview…</div> : <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">{items.map((item) => <PreviewCard key={item.id} item={item} session={session} locale={locale} onOpen={() => { setParent(item.id); load(session, item.id); }} />)}{!items.length && <div className="console-card col-span-full rounded-2xl p-8 text-sm console-muted">No indexed entries yet. Check the scan job on the Libraries tab.</div>}</div>}</div>;
}

function PreviewCard({ item, session, locale, onOpen }: { item: Item; session: Session | null; locale: string; onOpen: () => void }) {
	const [image, setImage] = useState<string | null>(null);
	useEffect(() => { let url = ""; if (session) adminFetch(`/api/admin/library-items/${item.id}/image?imageType=${item.type === "artist" || item.type === "release" ? "poster" : "poster"}&locale=${encodeURIComponent(locale)}`, session).then(async (response) => { if (response.ok) { url = URL.createObjectURL(await response.blob()); setImage(url); } }); return () => { if (url) URL.revokeObjectURL(url); }; }, [item.id, session, locale, item.type]);
	return <button onClick={onOpen} className="console-card group overflow-hidden rounded-2xl text-left transition hover:-translate-y-0.5"><div className="flex aspect-[2/3] items-center justify-center bg-black/30">{image ? <img src={image} alt="" className="h-full w-full object-cover transition group-hover:scale-105" /> : <IconPhoto className="console-muted" size={28} />}</div><div className="p-4"><p className="truncate font-semibold">{item.metadata?.title || item.displayName}</p><p className="mt-1 truncate text-xs console-muted">{item.type} · {item.matchStatus}</p><p className="mt-3 flex items-center justify-end text-xs text-[#8fe4cf]"><IconChevronRight size={15} /></p></div></button>;
}

export default function LibraryPreviewPage() { return <Suspense fallback={<div className="console-card rounded-2xl p-8 text-sm console-muted">Loading preview…</div>}><PreviewContent /></Suspense>; }
