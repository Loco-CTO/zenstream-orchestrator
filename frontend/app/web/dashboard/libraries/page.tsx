"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { IconFolder, IconPlus, IconRefresh, IconTrash } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";

type Library = { id: string; name: string; type: string; directory?: string | null; watchEnabled: boolean; scanIntervalMinutes: number; scanState: string; scanError?: string | null; jobId?: string; sourceLibraryIds?: string[] };

const labels: Record<string, string> = { tv_series: "TV Series", movies: "Movies", music: "Music", collection: "Collection" };

export default function LibrariesPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [libraries, setLibraries] = useState<Library[]>([]);
	const [name, setName] = useState("");
	const [type, setType] = useState("tv_series");
	const [directory, setDirectory] = useState("");
	const [sources, setSources] = useState<string[]>([]);
	const [watch, setWatch] = useState(true);
	const [interval, setIntervalValue] = useState(1440);
	const [message, setMessage] = useState("");

	async function load(current = session) {
		if (!current) return;
		const response = await adminFetch("/api/admin/libraries", current);
		if (response.ok) setLibraries(await response.json());
	}
	useEffect(() => { const current = readSession(); setSession(current); if (current) load(current); }, []);

	async function create(event: FormEvent) {
		event.preventDefault();
		if (!session) return;
		const response = await adminFetch("/api/admin/libraries", session, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, type, directory: type === "collection" ? null : directory, sourceLibraryIds: type === "collection" ? sources : [], watchEnabled: watch, scanIntervalMinutes: interval }) });
		if (response.ok) { setMessage("Library created and scan queued."); setName(""); setDirectory(""); setSources([]); load(); }
		else setMessage((await response.json().catch(() => null))?.detail || "Could not create library.");
	}

	async function rescan(library: Library) {
		if (!session) return;
		const response = await adminFetch(`/api/admin/libraries/${library.id}/scan`, session, { method: "POST" });
		setMessage(response.ok ? `Scan queued for ${library.name}.` : "Could not queue scan.");
		load();
	}
	async function remove(library: Library) {
		if (!session || !window.confirm(`Remove ${library.name} from ZenStream? Media files will not be deleted.`)) return;
		const response = await adminFetch(`/api/admin/libraries/${library.id}`, session, { method: "DELETE" });
		setMessage(response.ok ? "Library removed; media files were left untouched." : "Could not remove library.");
		load();
	}

	return <div>
		<p className="console-kicker">Libraries</p>
		<h1 className="mt-3 text-4xl font-black tracking-tight">Media inventory</h1>
		<p className="mt-2 max-w-2xl text-sm leading-6 console-muted">Index existing Jellyfin-compatible folders in place. Scans are read-only and never rename or reorganize your files.</p>
		<div className="mt-8 grid gap-6 xl:grid-cols-[1.35fr_.9fr]">
			<section className="console-card overflow-hidden rounded-2xl"><div className="flex items-center justify-between border-b console-divider px-5 py-4"><span className="console-kicker">Configured libraries</span><span className="text-xs console-muted">{libraries.length} total</span></div>{libraries.length ? libraries.map((library) => <article key={library.id} className="border-b console-divider px-5 py-5 last:border-0"><div className="flex items-start justify-between gap-4"><div className="flex min-w-0 items-start gap-3"><span className="rounded-xl bg-[#55c9b0]/10 p-2.5 text-[#8fe4cf]"><IconFolder size={20} /></span><div className="min-w-0"><p className="font-semibold">{library.name}</p><p className="mt-1 text-xs console-muted">{labels[library.type]} {library.directory ? `· ${library.directory}` : "· derived from source libraries"}</p><p className="mt-2 text-xs console-muted">{library.scanState === "scanning" ? "Scanning…" : library.scanState === "error" ? library.scanError : library.scanState === "ready" ? "Ready" : "Waiting for first scan"} · {library.watchEnabled ? "watching" : "watch disabled"}</p></div></div><div className="flex shrink-0 gap-2"><Link href={`/web/dashboard/libraries/preview?libraryId=${encodeURIComponent(library.id)}`} className="rounded-lg border console-divider px-3 py-2 text-xs text-[#8fe4cf]">Preview</Link><button onClick={() => rescan(library)} aria-label="Rescan" className="rounded-lg border console-divider p-2 console-muted hover:bg-white/10"><IconRefresh size={16} /></button><button onClick={() => remove(library)} aria-label="Remove" className="rounded-lg border console-divider p-2 console-muted hover:bg-red-400/10 hover:text-red-200"><IconTrash size={16} /></button></div></div></article>) : <div className="px-5 py-12 text-center text-sm console-muted">No libraries yet. Add your first media root.</div>}</section>
			<form onSubmit={create} className="console-card rounded-2xl p-6"><p className="console-kicker">New library</p><h2 className="mt-2 text-xl font-bold">Add media source</h2><input required value={name} onChange={(event) => setName(event.target.value)} placeholder="Library name" className="console-input mt-5 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30" /><select value={type} onChange={(event) => setType(event.target.value)} className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none"><option value="tv_series">TV Series</option><option value="movies">Movies</option><option value="music">Music</option><option value="collection">Collection</option></select>{type !== "collection" ? <input required value={directory} onChange={(event) => setDirectory(event.target.value)} placeholder="Server directory, e.g. X:\\Media Library\\Anime" className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30" /> : <div className="mt-3 rounded-xl border console-divider p-3 text-xs console-muted">Select Movie or TV libraries below. Collections are virtual and have no directory.</div>}{type === "collection" && <div className="mt-3 space-y-2">{libraries.filter((library) => library.type === "movies" || library.type === "tv_series").map((library) => <label key={library.id} className="flex items-center gap-3 rounded-lg border console-divider px-3 py-2 text-sm"><input type="checkbox" checked={sources.includes(library.id)} onChange={(event) => setSources((current) => event.target.checked ? [...current, library.id] : current.filter((id) => id !== library.id))} />{library.name}</label>)}</div>}<label className="mt-4 flex items-center gap-3 text-sm"><input type="checkbox" checked={watch} onChange={(event) => setWatch(event.target.checked)} />Watch for file changes</label>{type !== "collection" && <label className="mt-4 block text-sm"><span className="console-muted">Repair scan interval (minutes)</span><input type="number" min={15} max={43200} value={interval} onChange={(event) => setIntervalValue(Number(event.target.value))} className="console-input mt-2 h-11 w-full rounded-xl px-4 text-sm outline-none" /></label>}<button className="console-button mt-5 flex items-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold"><IconPlus size={17} />Create and scan</button>{message && <p className="mt-4 text-sm text-[#8fe4cf]">{message}</p>}</form>
		</div>
	</div>;
}
