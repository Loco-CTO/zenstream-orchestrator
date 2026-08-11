"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import {
	IconFolder,
	IconPlus,
	IconRefresh,
	IconTrash,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	ConfirmDialog,
	EmptyState,
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type Library = {
	id: string;
	name: string;
	type: string;
	directory?: string | null;
	watchEnabled: boolean;
	watchMode: "auto" | "native" | "polling";
	safetyScanEnabled: boolean;
	watcherStatus?: {
		state: string;
		backend?: string | null;
		capability?: string;
		nativeImplementation?: string | null;
		lastEventAt?: string | null;
		catchupState?: string;
		pendingRootCount?: number;
		pollIntervalSeconds?: number;
	};
	scanIntervalMinutes: number;
	scanState: string;
	scanError?: string | null;
	sourceLibraryIds?: string[];
};
const labels: Record<string, string> = {
	tv_series: "TV Series",
	movies: "Movies",
	music: "Music",
	collection: "Collection",
};

export default function LibrariesPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [libraries, setLibraries] = useState<Library[]>([]);
	const [name, setName] = useState("");
	const [type, setType] = useState("tv_series");
	const [directory, setDirectory] = useState("");
	const [sources, setSources] = useState<string[]>([]);
	const [watch, setWatch] = useState(true);
	const [watchMode, setWatchMode] = useState<"auto" | "native" | "polling">(
		"auto",
	);
	const [safetyScan, setSafetyScan] = useState(true);
	const [interval, setIntervalValue] = useState(1440);
	const [message, setMessage] = useState("");
	const [libraryToRemove, setLibraryToRemove] = useState<Library | null>(null);
	const [watcherTest, setWatcherTest] = useState<string | null>(null);

	async function load(current = session) {
		if (!current) return;
		const response = await adminFetch("/api/admin/libraries", current);
		if (response.ok) setLibraries(await response.json());
	}
	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) load(current);
	}, []);

	async function create(event: FormEvent) {
		event.preventDefault();
		if (!session) return;
		const response = await adminFetch("/api/admin/libraries", session, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				name,
				type,
				directory: type === "collection" ? null : directory,
				sourceLibraryIds: type === "collection" ? sources : [],
				watchEnabled: watch,
				watchMode,
				safetyScanEnabled: safetyScan,
				scanIntervalMinutes: interval,
			}),
		});
		setMessage(
			response.ok
				? "Library created and scan queued."
				: (await response.json().catch(() => null))?.detail ||
						"Could not create library.",
		);
		if (response.ok) {
			setName("");
			setDirectory("");
			setSources([]);
			load();
		}
	}
	async function rescan(library: Library) {
		if (!session) return;
		const response = await adminFetch(
			"/api/admin/libraries/" + library.id + "/scan",
			session,
			{ method: "POST" },
		);
		setMessage(
			response.ok
				? "Scan queued for " + library.name + "."
				: "Could not queue scan.",
		);
		load();
	}
	async function updateSettings(
		library: Library,
		values: Partial<
			Pick<Library, "watchEnabled" | "watchMode" | "safetyScanEnabled">
		>,
	) {
		if (!session) return;
		const response = await adminFetch(
			"/api/admin/libraries/" + library.id,
			session,
			{
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(values),
			},
		);
		setMessage(
			response.ok
				? "Library watcher settings updated."
				: "Could not update library settings.",
		);
		if (response.ok) load();
	}
	async function remove(library: Library) {
		if (!session) return;
		const response = await adminFetch(
			"/api/admin/libraries/" + library.id,
			session,
			{ method: "DELETE" },
		);
		setMessage(
			response.ok
				? "Library removed; files were left untouched."
				: "Could not remove library.",
		);
		setLibraryToRemove(null);
		load();
	}
	async function testWatcher(library: Library) {
		if (!session) return;
		const response = await adminFetch(
			`/api/admin/libraries/${library.id}/watcher-test`,
			session,
			{ method: "POST" },
		);
		if (!response.ok) return setMessage("Could not start the real-time test.");
		const value = await response.json();
		setWatcherTest(value.testId);
		setMessage(
			"Add or rename a harmless file in this library within 30 seconds.",
		);
		const started = Date.now();
		const poll = async () => {
			if (!session || !value.testId) return;
			const result = await adminFetch(
				`/api/admin/libraries/${library.id}/watcher-test/${value.testId}`,
				session,
			);
			const body = await result.json().catch(() => null);
			if (body?.status === "verified") {
				setMessage("Real-time event received.");
				setWatcherTest(null);
				load();
				return;
			}
			if (Date.now() - started < 30000) return void setTimeout(poll, 1500);
			setMessage("No event observed; delta verification remains active.");
			setWatcherTest(null);
		};
		void poll();
	}

	return (
		<div className="max-w-6xl">
			<ConfirmDialog
				open={Boolean(libraryToRemove)}
				title="Remove library?"
				description={`Remove ${libraryToRemove?.name || "this library"} from ZenStream. Its media files will remain untouched on disk.`}
				confirmLabel="Remove library"
				destructive
				onClose={() => setLibraryToRemove(null)}
				onConfirm={() => libraryToRemove && void remove(libraryToRemove)}
			/>
			<PageHeader
				title="Media sources"
				description="Connect media roots, monitor scans, and assemble collection libraries."
				actions={
					<button
						onClick={() => load()}
						aria-label="Refresh libraries"
						className="material-icon-button"
						title="Refresh libraries"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			<div className="mt-7 grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
				<SurfaceCard className="overflow-hidden">
					<div className="border-b console-divider px-5 py-4 text-xs uppercase tracking-[.16em] console-muted">
						Configured libraries{" "}
						<span className="ml-2 normal-case tracking-normal">
							{libraries.length}
						</span>
					</div>
					{libraries.map((library) => (
						<article
							key={library.id}
							className="flex items-start justify-between gap-4 border-b console-divider px-5 py-5 last:border-0"
						>
							<div className="flex min-w-0 items-start gap-3">
								<span className="rounded-lg bg-[#aeb9ff]/10 p-2.5 text-[#aeb9ff]">
									<IconFolder size={18} />
								</span>
								<div className="min-w-0">
									<p className="font-medium">{library.name}</p>
									<p className="mt-1 truncate text-xs console-muted">
										{labels[library.type]}{" "}
										{library.directory
											? "· " + library.directory
											: "· derived collection"}
									</p>
									<p className="mt-2 text-xs console-muted">
										{library.scanState === "error"
											? library.scanError
											: library.scanState === "ready"
												? "Ready"
												: library.scanState === "scanning"
													? "Scanning…"
													: "Waiting for first scan"}{" "}
										·{" "}
										{library.watcherStatus?.state === "active"
											? library.watcherStatus.capability === "verified"
												? "Verified real-time"
												: library.watcherStatus.backend === "delta"
													? `Delta-only verification (${library.watcherStatus.pollIntervalSeconds || 60}s)`
													: "Listening — unverified"
											: library.watchEnabled
												? library.watcherStatus?.state || "starting"
												: "watch disabled"}
									</p>
									{library.type !== "collection" && library.watchEnabled && (
										<div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] console-muted">
											<span>
												{library.watcherStatus?.nativeImplementation ||
													"Native backend pending"}
											</span>
											<span>
												· catch-up {library.watcherStatus?.catchupState || "pending"}
											</span>
											<span>
												· {library.watcherStatus?.pendingRootCount || 0} pending roots
											</span>
											<button
												className="text-[#aeb9ff]"
												onClick={() => void testWatcher(library)}
												disabled={Boolean(watcherTest)}
											>
												{watcherTest ? "Listening…" : "Test real-time"}
											</button>
										</div>
									)}
									{library.type !== "collection" && (
										<div className="mt-3 flex flex-wrap items-center gap-3 text-xs console-muted">
											<label className="flex items-center gap-2">
												<input
													type="checkbox"
													checked={library.watchEnabled}
													onChange={(event) =>
														void updateSettings(library, {
															watchEnabled: event.target.checked,
														})
													}
												/>
												Watch changes
											</label>
											<select
												value={library.watchMode}
												onChange={(event) =>
													void updateSettings(library, {
														watchMode: event.target.value as Library["watchMode"],
													})
												}
												className="console-input h-8 rounded-md px-2"
												aria-label="Watcher backend"
											>
												<option value="auto">Automatic backend</option>
												<option value="native">Native events</option>
												<option value="polling">Delta-only verification</option>
											</select>
											<label className="flex items-center gap-2">
												<input
													type="checkbox"
													checked={library.safetyScanEnabled}
													onChange={(event) =>
														void updateSettings(library, {
															safetyScanEnabled: event.target.checked,
														})
													}
												/>
												Periodic change verification
											</label>
										</div>
									)}
								</div>
							</div>
							<div className="flex shrink-0 gap-2">
								<Link
									href={
										"/web/dashboard/libraries/view?libraryId=" +
										encodeURIComponent(library.id)
									}
									className="rounded-lg border console-divider px-3 py-2 text-xs text-[#aeb9ff]"
								>
									View
								</Link>
								<button
									onClick={() => rescan(library)}
									aria-label="Rescan"
									className="rounded-lg border console-divider p-2 console-muted hover:bg-white/10"
								>
									<IconRefresh size={16} />
								</button>
								<button
									onClick={() => setLibraryToRemove(library)}
									aria-label="Remove"
									className="rounded-lg border console-divider p-2 console-muted hover:bg-[#aeb9ff]/10 hover:text-[#aeb9ff]"
								>
									<IconTrash size={16} />
								</button>
							</div>
						</article>
					))}
					{!libraries.length && (
						<EmptyState>No libraries yet. Add your first media root.</EmptyState>
					)}
				</SurfaceCard>
				<form onSubmit={create} className="console-card rounded-xl p-5">
					<p className="console-kicker">New library</p>
					<h2 className="mt-2 text-lg font-semibold">Add source</h2>
					<input
						required
						value={name}
						onChange={(event) => setName(event.target.value)}
						placeholder="Library name"
						className="console-input mt-5 h-11 w-full rounded-lg px-3 text-sm outline-none"
					/>
					<select
						value={type}
						onChange={(event) => setType(event.target.value)}
						className="console-input mt-3 h-11 w-full rounded-lg px-3 text-sm outline-none"
					>
						<option value="tv_series">TV Series</option>
						<option value="movies">Movies</option>
						<option value="music">Music</option>
						<option value="collection">Collection</option>
					</select>
					{type !== "collection" ? (
						<input
							required
							value={directory}
							onChange={(event) => setDirectory(event.target.value)}
							placeholder="X:\\Media Library\\Movies"
							className="console-input mt-3 h-11 w-full rounded-lg px-3 text-sm outline-none"
						/>
					) : (
						<div className="mt-3 rounded-lg border console-divider p-3 text-xs console-muted">
							Select existing TV or Movie libraries as collection sources.
						</div>
					)}
					{type === "collection" && (
						<div className="mt-3 space-y-2">
							{libraries
								.filter(
									(library) => library.type === "movies" || library.type === "tv_series",
								)
								.map((library) => (
									<label key={library.id} className="flex items-center gap-2 text-sm">
										<input
											type="checkbox"
											checked={sources.includes(library.id)}
											onChange={(event) =>
												setSources((current) =>
													event.target.checked
														? [...current, library.id]
														: current.filter((id) => id !== library.id),
												)
											}
										/>
										{library.name}
									</label>
								))}
						</div>
					)}
					<label className="mt-4 flex items-center justify-between text-sm">
						<span className="console-muted">Watch file changes</span>
						<input
							type="checkbox"
							checked={watch}
							onChange={(event) => setWatch(event.target.checked)}
						/>
					</label>
					{type !== "collection" && watch && (
						<label className="mt-4 block text-sm">
							<span className="console-muted">Watcher backend</span>
							<select
								value={watchMode}
								onChange={(event) =>
									setWatchMode(event.target.value as "auto" | "native" | "polling")
								}
								className="console-input mt-2 h-11 w-full rounded-lg px-3 outline-none"
							>
								<option value="auto">Automatic (recommended)</option>
								<option value="native">Native events</option>
								<option value="polling">Delta verification every 60 seconds</option>
							</select>
						</label>
					)}
					<label className="mt-4 flex items-center justify-between text-sm">
						<span className="console-muted">Periodic change verification</span>
						<input
							type="checkbox"
							checked={safetyScan}
							onChange={(event) => setSafetyScan(event.target.checked)}
						/>
					</label>
					{type !== "collection" && (
						<label className="mt-4 block text-sm">
							<span className="console-muted">Repair interval (minutes)</span>
							<input
								type="number"
								min={15}
								max={43200}
								value={interval}
								onChange={(event) => setIntervalValue(Number(event.target.value))}
								className="console-input mt-2 h-11 w-full rounded-lg px-3 outline-none"
							/>
						</label>
					)}
					<button className="console-button mt-5 flex items-center gap-2 rounded-lg px-4 py-3 text-sm font-medium">
						<IconPlus size={16} />
						Create and scan
					</button>
				</form>
			</div>
		</div>
	);
}
