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
import { activeStates, Job, JobTrigger, stateColor } from "./job-types";

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
				triggers: selected.triggers || [],
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
						<div className="mt-6 border-t console-divider pt-5">
							<div className="flex items-center justify-between">
								<div>
									<p className="console-kicker">Triggers</p>
									<p className="mt-1 text-xs console-muted">Use one or more schedules, or leave empty for manual-only runs.</p>
								</div>
								<button
									type="button"
									className="rounded-lg border border-[#5ee3d8]/25 px-3 py-2 text-xs text-[#5ee3d8]"
									onClick={() => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, triggers: [...(job.triggers || []), { id: crypto.randomUUID(), type: "interval", intervalSeconds: Math.max(60, job.intervalMinutes * 60) }] } : job))}
								>
									Add trigger
								</button>
							</div>
							<div className="mt-3 space-y-2">
								{(selected.triggers || []).map((trigger, index) => (
									<div key={trigger.id} className="flex items-center gap-2 rounded-lg border console-divider p-2 text-xs">
										<select
											value={trigger.type}
											className="console-input h-9 flex-1 rounded-md px-2"
											onChange={(event) => setJobs((current) => current.map((job) => {
												if (job.id !== selected.id) return job;
												const nextType = event.target.value as JobTrigger["type"];
												const next: JobTrigger = nextType === "interval"
													? { id: trigger.id, type: "interval", intervalSeconds: 1800 }
													: nextType === "daily"
														? { id: trigger.id, type: "daily", time: "02:00" }
														: nextType === "weekly"
															? { id: trigger.id, type: "weekly", weekday: 1, time: "02:00" }
															: { id: trigger.id, type: "startup" };
												return { ...job, triggers: job.triggers.map((value, valueIndex) => valueIndex === index ? next : value) };
											}))}
										>
											<option value="interval">Interval</option>
											<option value="daily">Daily</option>
											<option value="weekly">Weekly</option>
											<option value="startup">Startup</option>
										</select>
										{trigger.type === "interval" && <input type="number" min={1} max={2592000} value={trigger.intervalSeconds} aria-label="Interval seconds" className="console-input h-9 w-28 rounded-md px-2" onChange={(event) => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, triggers: job.triggers.map((value, valueIndex) => valueIndex === index ? { ...value, intervalSeconds: Number(event.target.value) } : value) } : job))} />}
										{(trigger.type === "daily" || trigger.type === "weekly") && <input type="time" value={trigger.time} aria-label="Trigger time" className="console-input h-9 w-28 rounded-md px-2" onChange={(event) => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, triggers: job.triggers.map((value, valueIndex) => valueIndex === index ? { ...value, time: event.target.value } : value) } : job))} />}
										{trigger.type === "weekly" && <select value={trigger.weekday} aria-label="Trigger weekday" className="console-input h-9 w-24 rounded-md px-2" onChange={(event) => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, triggers: job.triggers.map((value, valueIndex) => valueIndex === index ? { ...value, weekday: Number(event.target.value) } : value) } : job))}><option value={0}>Sun</option><option value={1}>Mon</option><option value={2}>Tue</option><option value={3}>Wed</option><option value={4}>Thu</option><option value={5}>Fri</option><option value={6}>Sat</option></select>}
										<button type="button" aria-label="Remove trigger" className="px-2 text-[#f07070]" onClick={() => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, triggers: job.triggers.filter((_, valueIndex) => valueIndex !== index) } : job))}>×</button>
									</div>
								))}
							</div>
						</div>
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
