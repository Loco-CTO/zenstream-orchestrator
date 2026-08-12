"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
	IconArrowLeft,
	IconClock,
	IconPlayerPlay,
	IconPlayerStop,
	IconRefresh,
	IconSettings,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	ConfirmDialog,
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";
import { activeStates, Job, stateColor } from "./job-types";

export default function JobDetailPage() {
	const params = useSearchParams();
	const requestedId = params.get("jobId") || "";
	const [session, setSession] = useState<Session | null>(null);
	const [jobs, setJobs] = useState<Job[]>([]);
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [message, setMessage] = useState("");
	const [confirmTerminate, setConfirmTerminate] = useState(false);
	const selected = useMemo(
		() => jobs.find((job) => job.id === requestedId),
		[jobs, requestedId],
	);
	const activeRun = selected?.recentRuns?.find((run) =>
		activeStates.has(run.state),
	);

	const load = useCallback(async (current: Session, showLoading = true) => {
		if (showLoading) setLoading(true);
		const response = await adminFetch("/api/admin/jobs", current);
		if (response.ok)
			setJobs(((await response.json()) as { jobs?: Job[] }).jobs || []);
		if (showLoading) setLoading(false);
	}, []);

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) void load(current);
	}, [load]);

	useEffect(() => {
		if (
			!session ||
			!selected?.recentRuns?.some((run) => activeStates.has(run.state))
		)
			return;
		const timer = window.setInterval(() => void load(session, false), 2000);
		return () => window.clearInterval(timer);
	}, [load, selected, session]);

	async function save() {
		if (!session || !selected) return;
		setSaving(true);
		const response = await adminFetch(`/api/admin/jobs/${selected.id}`, session, {
			method: "PATCH",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				intervalMinutes: selected.intervalMinutes,
				enabled: selected.enabled,
				config: selected.config || {},
			}),
		});
		setMessage(
			response.ok ? "Task settings saved." : "Could not save task settings.",
		);
		if (response.ok) await load(session, false);
		setSaving(false);
	}

	async function runNow() {
		if (!session || !selected) return;
		const response = await adminFetch(
			`/api/admin/jobs/${selected.id}/run`,
			session,
			{ method: "POST" },
		);
		setMessage(
			response.ok ? "Task queued in the background." : "Could not queue task.",
		);
		await load(session, false);
	}

	async function terminate() {
		if (!session || !selected || !activeRun) return;
		const response = await adminFetch(
			`/api/admin/jobs/${selected.id}/runs/${activeRun.id}/terminate`,
			session,
			{ method: "POST" },
		);
		setMessage(
			response.ok ? "Termination requested." : "Could not terminate the task run.",
		);
		setConfirmTerminate(false);
		await load(session, false);
	}

	if (!requestedId) {
		return (
			<div className="dashboard-page">
				<PageHeader
					title="Task not selected"
					description="Choose a scheduled task to inspect its settings and recent runs."
				/>
				<Link href="/web/dashboard/jobs" className="material-back">
					<IconArrowLeft size={16} /> Back to tasks
				</Link>
			</div>
		);
	}
	if (!loading && !selected) {
		return (
			<div className="dashboard-page">
				<PageHeader
					title="Task not found"
					description="This task may have been removed or is not available to your administrator account."
				/>
				<Link href="/web/dashboard/jobs" className="material-back">
					<IconArrowLeft size={16} /> Back to tasks
				</Link>
			</div>
		);
	}

	return (
		<div className="dashboard-page">
			<ConfirmDialog
				open={confirmTerminate}
				title="Terminate active task run?"
				description={`This stops the active run for ${selected?.name || "this task"}. Incomplete work may be resumed by its scheduler later.`}
				confirmLabel="Terminate run"
				destructive
				onClose={() => setConfirmTerminate(false)}
				onConfirm={() => void terminate()}
			/>
			<div className="mb-6">
				<Link href="/web/dashboard/jobs" className="material-back">
					<IconArrowLeft size={16} /> Back to tasks
				</Link>
			</div>
			<PageHeader
				title={selected?.name || "Loading task"}
				description={
					selected?.description ||
					"Review scheduler settings, triggers, and recent runs."
				}
				actions={
					<button
						onClick={() => session && void load(session)}
						className="material-icon-button"
						aria-label="Refresh task"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			{selected && (
				<div className="mt-7 grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
					<SurfaceCard className="p-6">
						<div className="flex items-start justify-between gap-4">
							<div>
								<p className="console-kicker">Schedule</p>
								<h2 className="mt-2 text-xl font-semibold">Task controls</h2>
							</div>
							<IconSettings className="console-muted" size={19} />
						</div>
						<label className="mt-7 block text-sm">
							<span className="console-muted">Run every (minutes)</span>
							<input
								type="number"
								min={5}
								max={43200}
								value={selected.intervalMinutes}
								onChange={(event) =>
									setJobs((current) =>
										current.map((job) =>
											job.id === selected.id
												? { ...job, intervalMinutes: Number(event.target.value) }
												: job,
										),
									)
								}
								className="console-input mt-2 h-11 w-full rounded-lg px-3 outline-none"
							/>
						</label>
						<label className="mt-4 flex items-center justify-between text-sm">
							<span className="console-muted">Enabled</span>
							<input
								type="checkbox"
								checked={selected.enabled}
								onChange={(event) =>
									setJobs((current) =>
										current.map((job) =>
											job.id === selected.id
												? { ...job, enabled: event.target.checked }
												: job,
										),
									)
								}
							/>
						</label>
						{selected.kind === "metadata_refresh" && (
							<label className="mt-5 flex items-start justify-between gap-4 rounded-xl border console-divider p-3 text-sm">
								<span>
									<span className="block console-muted">
										Preserve cached artwork and portraits
									</span>
									<span className="mt-1 block text-xs leading-5 console-muted">
										Reuse valid files during metadata refreshes; missing or changed assets
										are still downloaded.
									</span>
								</span>
								<input
									type="checkbox"
									checked={Boolean(selected.config?.preserveCachedAssets)}
									onChange={(event) =>
										setJobs((current) =>
											current.map((job) =>
												job.id === selected.id
													? {
															...job,
															config: {
																...(job.config || {}),
																preserveCachedAssets: event.target.checked,
															},
														}
													: job,
											),
										)
									}
								/>
							</label>
						)}
						<div className="mt-6 grid gap-2 sm:grid-cols-2">
							<button
								onClick={() => void save()}
								disabled={saving}
								className="console-button rounded-lg px-3 py-2.5 text-sm font-medium disabled:opacity-50"
							>
								{saving ? "Saving…" : "Save settings"}
							</button>
							<button
								onClick={() => void runNow()}
								disabled={Boolean(activeRun)}
								className="flex items-center justify-center gap-2 rounded-lg border console-divider px-3 py-2.5 text-sm console-muted hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
							>
								<IconPlayerPlay size={15} />
								{activeRun ? "Task active" : "Run now"}
							</button>
							{activeRun && (
								<button
									onClick={() => setConfirmTerminate(true)}
									disabled={activeRun.state === "terminating"}
									className="sm:col-span-2 flex items-center justify-center gap-2 rounded-lg border border-[#f07070]/30 px-3 py-2.5 text-sm text-[#f07070] hover:bg-[#f07070]/10 disabled:opacity-50"
								>
									<IconPlayerStop size={15} />
									{activeRun.state === "terminating"
										? "Terminating…"
										: "Terminate active run"}
								</button>
							)}
						</div>
					</SurfaceCard>
					<SurfaceCard className="p-6">
						<p className="console-kicker">Recent activity</p>
						<div className="mt-2 flex items-center gap-2">
							<IconClock size={18} className="text-[#5ee3d8]" />
							<span
								className={`capitalize ${stateColor[selected.lastState] || "console-muted"}`}
							>
								{selected.enabled ? selected.lastState : "paused"}
							</span>
						</div>
						<div className="mt-7 space-y-4">
							{selected.recentRuns?.slice(0, 8).map((run) => (
								<div
									key={run.id}
									className="flex items-start justify-between gap-3 text-xs"
								>
									<span>
										<span
											className={`block capitalize ${stateColor[run.state] || "console-muted"}`}
										>
											{run.state}
										</span>
										<span className="mt-1 block console-muted">
											{run.message || run.error || "No details"}
										</span>
									</span>
									<time className="shrink-0 console-muted">
										{new Date(run.createdAt).toLocaleString()}
									</time>
								</div>
							))}
							{!selected.recentRuns?.length && (
								<p className="text-xs console-muted">No runs yet.</p>
							)}
						</div>
					</SurfaceCard>
				</div>
			)}
		</div>
	);
}
