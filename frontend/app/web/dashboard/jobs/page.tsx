"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
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

export default function JobsPage() {
	const router = useRouter();
	const params = useSearchParams();
	const [session, setSession] = useState<Session | null>(null);
	const [jobs, setJobs] = useState<Job[]>([]);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");

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
		if (!session || !jobs.some((job) => activeStates.has(job.lastState))) return;
		const timer = window.setInterval(() => void load(session), 2000);
		return () => window.clearInterval(timer);
	}, [jobs, load, session]);

	const groups = useMemo<TaskGroup[]>(
		() =>
			GROUP_ORDER.map((group) => ({
				group,
				tasks: jobs.filter((job) => taskGroup(job) === group),
			})).filter((group) => group.tasks.length),
		[jobs],
	);

	async function toggleRun(task: Job, event: React.MouseEvent) {
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
			await adminFetch(`/api/admin/jobs/${task.id}/run`, session, {
				method: "POST",
			});
		}
		await load(session);
	}

	return (
		<div className="dashboard-page dashboard-design">
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
								{group.tasks.map((task, index) => {
									const activeRun = task.recentRuns?.find((run) =>
										activeStates.has(run.state),
									);
									const active = Boolean(activeRun);
									const progress = progressFor(task);
									const progressDetail = progressDetailText(
										activeRun?.progressDetail,
									);
									return (
										<div key={task.id}>
											<div style={{ padding: "0 18px" }}>
												<div
													role="button"
													tabIndex={0}
													onClick={() =>
														router.push(
															`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(task.id)}`,
														)
													}
													onKeyDown={(event) => {
														if (event.key === "Enter" || event.key === " ") {
															event.preventDefault();
															router.push(
																`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(task.id)}`,
															);
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
															Last run {relativeTime(task.lastRunAt)} · {runDuration(task)}
														</div>
													</div>
													{!task.historyOnly && (
														<button
															type="button"
															onClick={(event) => void toggleRun(task, event)}
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
															{active ? (
																<IconPlayerStop size={12} />
															) : (
																<IconPlayerPlay size={12} />
															)}
														</button>
													)}
												</div>
												{active && progress !== undefined && (
													<div style={{ paddingBottom: 12 }}>
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
														<div
															style={{
																fontSize: 11,
																color: "#555",
																textAlign: "right",
																fontFamily: "var(--font-mono)",
															}}
														>
															{Math.round(progress)}%
														</div>
													</div>
												)}
											</div>
											{index < group.tasks.length - 1 && (
												<div style={{ height: 1, background: "#111", margin: 0 }} />
											)}
										</div>
									);
								})}
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
