"use client";

import { IconRefresh } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { adminFetch, readSession } from "../components/admin-client";
import { PageHeader, SurfaceCard } from "../components/dashboard-surface";

const categories = {
	media: ["Media", "Files in configured library folders"],
	metadata: ["Metadata", "Server database and extracted subtitles"],
	artwork: ["Artwork", "Local, provider, and generated image caches"],
	trickplay: ["Trickplay", "Playback preview thumbnails"],
	transcodes: ["Transcodes", "Temporary playback output"],
	offline: ["Offline assets", "Server-prepared downloads and bundles"],
	lumi: ["Lumi models", "Model files managed by the server"],
} as const;

type StorageOverview = {
	measuredAt: string;
	totalBytes: number;
	categories: {
		category: keyof typeof categories;
		bytes: number;
		unreadablePaths: number;
	}[];
};

function formatBytes(bytes: number) {
	const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
	let value = bytes;
	let unit = 0;
	while (value >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit += 1;
	}
	return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[unit]}`;
}

export default function StoragePage() {
	const [overview, setOverview] = useState<StorageOverview | null>(null);
	const [attempt, setAttempt] = useState(0);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");

	useEffect(() => {
		const session = readSession();
		if (!session) return;
		const controller = new AbortController();
		let retry: number | undefined;

		const load = async (refresh: boolean) => {
			try {
				const response = await adminFetch(
					refresh ? "/api/admin/storage?refresh=true" : "/api/admin/storage",
					session,
					{ signal: controller.signal },
				);
				if (controller.signal.aborted) return;
				if (response.status === 202) {
					const seconds = Number(response.headers.get("Retry-After")) || 2;
					retry = window.setTimeout(() => void load(false), seconds * 1000);
					return;
				}
				if (!response.ok) throw new Error("Storage usage could not be measured.");
				const result: StorageOverview = await response.json();
				if (controller.signal.aborted) return;
				setOverview(result);
			} catch {
				if (controller.signal.aborted) return;
				setError("Storage usage could not be measured. Try refreshing.");
			}
			setLoading(false);
		};

		void load(attempt > 0);
		return () => {
			controller.abort();
			window.clearTimeout(retry);
		};
	}, [attempt]);

	const partial = overview?.categories.some(
		(entry) => entry.unreadablePaths > 0,
	);

	return (
		<div className="dashboard-page dashboard-design">
			<PageHeader
				title="Server storage"
				actions={
					<button
						type="button"
						disabled={loading}
						onClick={() => {
							setLoading(true);
							setError("");
							setAttempt((previous) => previous + 1);
						}}
						className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-[var(--border-strong)] bg-[var(--surface)] px-3 text-xs font-medium text-[var(--text)] hover:bg-[var(--surface-2)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--primary)] disabled:opacity-60"
					>
						<IconRefresh size={15} aria-hidden="true" />
						Refresh
					</button>
				}
			/>
			<div role="status">
				{loading && (
					<p className="mb-4 text-sm text-[var(--text-2)]">
						Measuring server storage…
					</p>
				)}
			</div>
			{error && (
				<p role="alert" className="mb-4 text-sm text-[var(--danger)]">
					{error}
				</p>
			)}
			{overview && (
				<SurfaceCard>
					<div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
						<div>
							<h2 className="text-sm font-semibold text-[var(--text)]">
								Measured storage
							</h2>
							<p className="mt-1 text-3xl font-semibold tracking-tight tabular-nums">
								{formatBytes(overview.totalBytes)}
							</p>
						</div>
						<p role="status" className="text-xs text-[var(--text-2)]">
							Measured{" "}
							<time dateTime={overview.measuredAt}>
								{new Date(overview.measuredAt).toLocaleString(undefined, {
									dateStyle: "medium",
									timeStyle: "medium",
								})}
							</time>
						</p>
					</div>
					<p className="mt-2 text-xs text-[var(--text-2)]">
						Totals use file sizes in the locations ZenStream tracks.
					</p>
					{partial && (
						<p className="mt-3 text-sm text-[var(--warning)]">
							Some locations could not be read. Totals include the files that were
							measured.
						</p>
					)}
					<dl className="mt-4 divide-y divide-[var(--border)] border-t border-[var(--border)]">
						{overview.categories.map(({ category, bytes, unreadablePaths }) => {
							const [label, description] = categories[category];
							const share = overview.totalBytes
								? (bytes / overview.totalBytes) * 100
								: 0;
							return (
								<div
									key={category}
									className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-2 py-3"
								>
									<dt className="text-sm font-medium">
										{label}
										<span className="mt-1 block text-xs font-normal text-[var(--text-2)]">
											{description}
										</span>
									</dt>
									<dd className="text-right text-sm font-medium tabular-nums">
										{unreadablePaths > 0 && bytes === 0 ? (
											<span className="text-xs text-[var(--warning)]">Unavailable</span>
										) : (
											formatBytes(bytes)
										)}
										{unreadablePaths > 0 && bytes > 0 && (
											<span className="block text-xs text-[var(--warning)]">Partial</span>
										)}
									</dd>
									<dd
										aria-hidden="true"
										className="col-span-2 h-1 overflow-hidden rounded-full bg-[var(--surface-3)]"
									>
										<div
											className="h-full rounded-full bg-[var(--primary)]"
											style={{ width: `${share}%` }}
										/>
									</dd>
								</div>
							);
						})}
					</dl>
				</SurfaceCard>
			)}
		</div>
	);
}
