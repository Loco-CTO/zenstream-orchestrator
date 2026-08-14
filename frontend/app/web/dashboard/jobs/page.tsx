"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
	IconClock,
	IconPlayerPlay,
	IconPlayerStop,
	IconRefresh,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import { Job, activeStates, progressDetailText } from "./job-types";

type TaskGroup = { group: string; tasks: Job[] };

const GROUP_ORDER = ["Catalog", "Library", "Media Analysis", "Metadata"];

function taskGroup(job: Job) {
	if (job.kind.includes("metadata")) return "Metadata";
	if (job.kind.includes("trickplay") || job.kind.includes("intro_outro"))
		return "Media Analysis";
	if (job.kind.includes("library")) return "Library";
	return "Catalog";
}

function relativeTime(value?: string | null) {
	if (!value) return "never";
	const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
	if (seconds < 60) return "just now";
	if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
	if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
	return `${Math.floor(seconds / 86400)} days ago`;
}

function runDuration(job: Job) {
	const run = job.recentRuns?.[0];
	if (!run?.startedAt) return "< 1 min";
	const end = run.finishedAt ? new Date(run.finishedAt).getTime() : Date.now();
	const seconds = Math.max(0, (end - new Date(run.startedAt).getTime()) / 1000);
	return seconds < 60 ? "< 1 min" : `${Math.round(seconds / 60)} min`;
}

function progressFor(job: Job) {
	const run = job.recentRuns?.find((entry) => activeStates.has(entry.state));
	if (!run || !run.progressTotal) return undefined;
	return Math.max(
		0,
		Math.min(100, (run.progressCurrent / run.progressTotal) * 100),
	);
}

type TaskRowProps = {
	task: Job;
	isLast: boolean;
	onOpen: (taskId: string) => void;
	onToggleRun: (task: Job, event: React.MouseEvent<HTMLButtonElement>) => void;
};

const TaskRow = memo(function TaskRow({
	task,
	isLast,
	onOpen,
	onToggleRun,
}: TaskRowProps) {
	const activeRun = task.recentRuns?.find((run) => activeStates.has(run.state));
	const active = Boolean(activeRun);
	const progress = progressFor(task);
	const progressDetail = progressDetailText(activeRun?.progressDetail);

	return (
		<div>
			<div style={{ padding: "0 18px" }}>
				<div
					role="button"
					tabIndex={0}
					onClick={() => onOpen(task.id)}
					onKeyDown={(event) => {
						if (event.key === "Enter" || event.key === " ") {
							event.preventDefault();
							onOpen(task.id);
						}
					}}
					style={{
						display: "flex",
						alignItems: "center",
						gap: 14,
						paddingTop: 13,
						paddingBottom: active ? 8 : 13,
						cursor: "pointer",
					}}
				>
					<div
						style={{
							width: 32,
							height: 32,
							borderRadius: "50%",
							background: "var(--primary-dim)",
							border: "1px solid rgba(94,227,216,0.15)",
							display: "flex",
							alignItems: "center",
							justifyContent: "center",
							flexShrink: 0,
							color: "var(--primary)",
						}}
					>
						<IconClock size={14} />
					</div>
					<div style={{ flex: 1, minWidth: 0 }}>
						<div style={{ fontSize: 14, fontWeight: 500, color: "#ddd" }}>
							{task.name}
						</div>
						<div style={{ fontSize: 11, color: "#555", marginTop: 2 }}>
							{activeRun
								? activeRun.message || activeRun.state
								: `Last run ${relativeTime(task.lastRunAt)} · ${runDuration(task)}`}
						</div>
					</div>
					{!task.historyOnly && (
						<button
							type="button"
							onClick={(event) => onToggleRun(task, event)}
							aria-label={active ? `Stop ${task.name}` : `Run ${task.name}`}
							style={{
								width: 28,
								height: 28,
								borderRadius: 6,
								background: "none",
								border: "none",
								color: active ? "#888" : "#444",
								cursor: "pointer",
								display: "flex",
								alignItems: "center",
								justifyContent: "center",
								flexShrink: 0,
							}}
						>
							{active ? <IconPlayerStop size={12} /> : <IconPlayerPlay size={12} />}
						</button>
					)}
				</div>
				{active && (
					<div style={{ paddingBottom: 12 }}>
						{progress === undefined ? (
							<progress
								aria-label={`${task.name} preparation progress`}
								style={{
									width: "100%",
									height: 3,
									marginBottom: 5,
									accentColor: "var(--primary)",
								}}
							/>
						) : (
							<div
								style={{
									height: 2,
									background: "#111",
									borderRadius: 2,
									overflow: "hidden",
									marginBottom: 5,
								}}
							>
								<div
									style={{
										height: "100%",
										width: `${progress}%`,
										background: "var(--primary)",
										borderRadius: 2,
										transition: "width 0.4s ease",
									}}
								/>
							</div>
						)}
						<div
							style={{
								display: "flex",
								alignItems: "center",
								justifyContent: "space-between",
								gap: 12,
								fontSize: 11,
								color: "#555",
								fontFamily: "var(--font-mono)",
							}}
						>
							<span
								style={{
									minWidth: 0,
									flex: 1,
									overflow: "hidden",
									textOverflow: "ellipsis",
									whiteSpace: "nowrap",
								}}
							>
								{progressDetail}
							</span>
							<span style={{ flexShrink: 0 }}>
								{progress === undefined ? "Preparing…" : `${Math.round(progress)}%`}
							</span>
						</div>
					</div>
				)}
			</div>
			{!isLast && <div style={{ height: 1, background: "#111", margin: 0 }} />}
		</div>
	);
});

export default function JobsPage() {
	const router = useRouter();
	const params = useSearchParams();
	const [session, setSession] = useState<Session | null>(null);
	const [jobs, setJobs] = useState<Job[]>([]);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [runTask, setRunTask] = useState<Job | null>(null);
	const [runOptions, setRunOptions] = useState<Record<string, unknown>>({});

	const load = useCallback(async (current: Session | null) => {
		if (!current) return;
		setLoading(true);
		const response = await adminFetch("/api/admin/jobs", current);
		if (response.ok) {
			setJobs(((await response.json()) as { jobs?: Job[] }).jobs || []);
			setError("");
		} else {
			setError("Scheduled tasks could not be loaded.");
		}
		setLoading(false);
	}, []);

	const refreshTask = useCallback(async (current: Session | null, taskId: string) => {
		if (!current) return null;
		const response = await adminFetch(
			`/api/admin/jobs/${encodeURIComponent(taskId)}`,
			current,
		);
		if (!response.ok) return null;
		return (await response.json()) as Job;
	}, []);

	const refreshActiveTasks = useCallback(
		async (current: Session, taskIds: string[]) => {
			const results = await Promise.allSettled(
				taskIds.map((taskId) => refreshTask(current, taskId)),
			);
			const updates = results.flatMap((result) =>
				result.status === "fulfilled" && result.value ? [result.value] : [],
			);
			if (!updates.length) return;
			const byId = new Map(updates.map((task) => [task.id, task]));
			setJobs((currentJobs) =>
				currentJobs.map((task) => byId.get(task.id) || task),
			);
		},
		[refreshTask],
	);

	useEffect(() => {
		const legacyId = params.get("jobId");
		if (legacyId) {
			router.replace(
				`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(legacyId)}`,
			);
			return;
		}
		const current = readSession();
		setSession(current);
		void load(current);
	}, [load, params, router]);

	useEffect(() => {
		if (!session) return;
		const activeTaskIds = jobs
			.filter((job) => job.recentRuns?.some((run) => activeStates.has(run.state)))
			.map((job) => job.id);
		if (!activeTaskIds.length) return;
		let polling = false;
		const poll = async () => {
			if (polling) return;
			polling = true;
			try {
				await refreshActiveTasks(session, activeTaskIds);
			} finally {
				polling = false;
			}
		};
		const timer = window.setInterval(() => void poll(), 2000);
		return () => window.clearInterval(timer);
	}, [jobs, refreshActiveTasks, session]);

	const groups = useMemo<TaskGroup[]>(
		() =>
			GROUP_ORDER.map((group) => ({
				group,
				tasks: jobs.filter((job) => taskGroup(job) === group),
			})).filter((group) => group.tasks.length),
		[jobs],
	);

	const openTask = useCallback(
		(taskId: string) => {
			router.push(
				`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(taskId)}`,
			);
		},
		[router],
	);

	const toggleRun = useCallback(
		async (task: Job, event: React.MouseEvent<HTMLButtonElement>) => {
		event.stopPropagation();
		if (task.historyOnly) return;
		if (!session) return;
		const activeRun = task.recentRuns?.find((run) => activeStates.has(run.state));
		if (activeRun) {
			await adminFetch(
				`/api/admin/jobs/${task.id}/runs/${activeRun.id}/terminate`,
				session,
				{ method: "POST" },
			);
		} else {
			if (task.optionDefinitions?.length) {
				setRunOptions(
					Object.fromEntries(
						task.optionDefinitions.map((item) => [item.key, item.default ?? false]),
					),
				);
				setRunTask(task);
				return;
			}
			await adminFetch(`/api/admin/jobs/${task.id}/run`, session, {
				method: "POST",
			});
		}
		const updated = await refreshTask(session, task.id);
		if (updated) {
			setJobs((current) =>
				current.map((item) => (item.id === updated.id ? updated : item)),
			);
		}
		},
		[refreshTask, session],
	);

	const confirmRun = useCallback(async () => {
		if (!session || !runTask) return;
		await adminFetch(`/api/admin/jobs/${runTask.id}/run`, session, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ options: runOptions }),
		});
		setRunTask(null);
		const updated = await refreshTask(session, runTask.id);
		if (updated) {
			setJobs((current) =>
				current.map((item) => (item.id === updated.id ? updated : item)),
			);
		}
	}, [refreshTask, runTask, session]);

	return (
		<div className="dashboard-page dashboard-design">
			{runTask && (
				<div
					role="presentation"
					onMouseDown={(event) =>
						event.target === event.currentTarget && setRunTask(null)
					}
					style={{
						position: "fixed",
						inset: 0,
						zIndex: 50,
						display: "flex",
						alignItems: "center",
						justifyContent: "center",
						background: "rgba(0,0,0,.72)",
						padding: 20,
					}}
				>
					<div
						role="dialog"
						aria-modal="true"
						style={{
							width: "100%",
							maxWidth: 420,
							background: "#101010",
							border: "1px solid var(--border-strong)",
							borderRadius: 12,
							padding: 22,
						}}
					>
						<h2 style={{ margin: "0 0 20px", fontSize: 16, color: "#fff" }}>
							Run {runTask.name}
						</h2>
						{runTask.optionDefinitions?.map((option) => (
							<label
								key={option.key}
								style={{
									display: "flex",
									gap: 8,
									color: "#aaa",
									fontSize: 12,
									marginBottom: 14,
								}}
							>
								<input
									type="checkbox"
									checked={Boolean(runOptions[option.key] ?? option.default)}
									onChange={(event) =>
										setRunOptions((current) => ({
											...current,
											[option.key]: event.target.checked,
										}))
									}
								/>
								{option.label}
							</label>
						))}
						<div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
							<button
								type="button"
								onClick={() => setRunTask(null)}
								className="rounded-lg border console-divider px-3 py-2 text-sm"
							>
								Cancel
							</button>
							<button
								type="button"
								onClick={() => void confirmRun()}
								className="console-button rounded-lg px-3 py-2 text-sm"
							>
								Run now
							</button>
						</div>
					</div>
				</div>
			)}
			<div
				style={{
					display: "flex",
					alignItems: "flex-start",
					justifyContent: "space-between",
					marginBottom: 36,
					gap: 16,
				}}
			>
				<div>
					<h1
						style={{
							margin: 0,
							fontSize: 22,
							fontWeight: 600,
							color: "#fff",
							letterSpacing: "-0.02em",
						}}
					>
						Tasks
					</h1>
					<p
						style={{
							margin: "5px 0 0",
							fontSize: 13,
							color: "#666",
							lineHeight: 1.5,
						}}
					>
						Review scheduled work, tune its cadence, and manage active runs.
					</p>
				</div>
				<button
					type="button"
					onClick={() => void load(session)}
					title="Refresh"
					aria-label="Refresh tasks"
					style={{
						background: "none",
						border: "none",
						color: "#777",
						cursor: "pointer",
						width: 32,
						height: 32,
						display: "flex",
						alignItems: "center",
						justifyContent: "center",
						borderRadius: 8,
					}}
				>
					<IconRefresh size={15} />
				</button>
			</div>

			{error && (
				<div
					role="alert"
					style={{ color: "var(--danger)", fontSize: 12, marginBottom: 16 }}
				>
					{error}
				</div>
			)}

			{loading ? (
				<div
					style={{
						background: "#080808",
						borderRadius: 12,
						padding: "20px 22px",
						color: "#666",
						fontSize: 13,
					}}
				>
					Loading tasks…
				</div>
			) : (
				<div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
					{groups.map((group) => (
						<div key={group.group}>
							<div
								style={{
									fontSize: 13,
									fontWeight: 600,
									color: "#fff",
									marginBottom: 10,
								}}
							>
								{group.group}
							</div>
							<div style={{ background: "#080808", borderRadius: 12, padding: 0 }}>
								{group.tasks.map((task, index) => (
									<TaskRow
										key={task.id}
										task={task}
										isLast={index === group.tasks.length - 1}
										onOpen={openTask}
										onToggleRun={toggleRun}
									/>
								))}
							</div>
						</div>
					))}
					{!groups.length && (
						<div
							style={{
								background: "#080808",
								borderRadius: 12,
								padding: "20px 22px",
								color: "#666",
								fontSize: 13,
							}}
						>
							No scheduled tasks.
						</div>
					)}
				</div>
			)}
		</div>
	);
}
