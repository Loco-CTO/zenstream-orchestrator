"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
	IconChevronRight,
	IconClock,
	IconPlayerPlay,
	IconPlayerStop,
	IconRefresh,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import { Job, activeStates } from "./job-types";

function relativeTime(value?: string | null) {
	if (!value) return "never";
	const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
	if (seconds < 60) return `${Math.floor(seconds / 60)} min ago`;
	if (seconds < 3600) return `${Math.floor(seconds / 3600)} hours ago`;
	if (seconds < 86400) return `${Math.floor(seconds / 86400)} days ago`;
	return `${Math.floor(seconds / 86400)} days ago`;
}

function duration(job: Job) {
	const run = job.recentRuns?.[0];
	if (!run?.startedAt) return "< 1 min";
	const end = run.finishedAt ? new Date(run.finishedAt).getTime() : Date.now();
	const seconds = Math.max(0, (end - new Date(run.startedAt).getTime()) / 1000);
	return seconds < 60 ? "< 1 min" : `${Math.round(seconds / 60)} min`;
}

function statusFor(job: Job) {
	if (!job.enabled) return { label: "Paused", color: "#666" };
	if (activeStates.has(job.lastState))
		return { label: "Running", color: "#60b4e8" };
	if (["failed", "error"].includes(job.lastState))
		return { label: "Failed", color: "#f07070" };
	return { label: "Completed", color: "#5ee3d8" };
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
		if (!response.ok) setError("Scheduled tasks could not be loaded.");
		else setJobs(((await response.json()) as { jobs?: Job[] }).jobs || []);
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
	async function runNow(job: Job) {
		if (!session) return;
		const response = await adminFetch(`/api/admin/jobs/${job.id}/run`, session, {
			method: "POST",
		});
		if (!response.ok) setError("The task could not be started.");
		else await load(session);
	}
	return (
		<div
			className="dashboard-page dashboard-design"
			style={{ width: "100%", maxWidth: 1268, margin: "0 auto" }}
		>
			<header
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
						width: 32,
						height: 32,
						border: "none",
						background: "none",
						color: "#777",
						display: "flex",
						alignItems: "center",
						justifyContent: "center",
						borderRadius: 8,
						cursor: "pointer",
					}}
				>
					<IconRefresh size={15} />
				</button>
			</header>
			{error && (
				<p
					role="alert"
					style={{ color: "var(--danger)", fontSize: 12, marginBottom: 16 }}
				>
					{error}
				</p>
			)}
			<section
				style={{ background: "#080808", borderRadius: 12, overflow: "hidden" }}
			>
				<div
					style={{
						padding: "36px 40px 20px",
						display: "flex",
						alignItems: "center",
						gap: 14,
					}}
				>
					<span
						style={{
							fontSize: 11,
							fontWeight: 600,
							color: "#888",
							letterSpacing: "0.1em",
							textTransform: "uppercase",
						}}
					>
						All tasks
					</span>
					<span style={{ fontSize: 12, color: "#5ee3d8" }}>{jobs.length || ""}</span>
				</div>
				<div style={{ height: 1, background: "#111" }} />
				{loading ? (
					<div style={{ padding: "28px 40px", fontSize: 13, color: "#666" }}>
						Loading tasks…
					</div>
				) : (
					jobs.map((job, index) => {
						const status = statusFor(job);
						const running = activeStates.has(job.lastState);
						const subtitle =
							job.description ||
							"Last run " + relativeTime(job.lastRunAt) + " · " + duration(job);
						return (
							<div key={job.id}>
								<div
									style={{
										display: "grid",
										gridTemplateColumns: "minmax(0, 1fr) 92px 32px 24px",
										alignItems: "center",
										columnGap: 18,
										minHeight: 68,
										padding: "0 40px",
									}}
								>
									<button
										type="button"
										onClick={() =>
											router.push(
												`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(job.id)}`,
											)
										}
										style={{
											minWidth: 0,
											display: "flex",
											alignItems: "center",
											gap: 14,
											textAlign: "left",
											border: 0,
											padding: 0,
											background: "none",
											color: "inherit",
											cursor: "pointer",
										}}
									>
										<span
											style={{
												width: 30,
												height: 30,
												borderRadius: "50%",
												background: "rgba(94,227,216,.09)",
												border: "1px solid rgba(94,227,216,.14)",
												display: "flex",
												alignItems: "center",
												justifyContent: "center",
												flexShrink: 0,
												color: "#5ee3d8",
											}}
										>
											<IconClock size={14} />
										</span>
										<span style={{ minWidth: 0 }}>
											<span
												style={{
													display: "block",
													overflow: "hidden",
													textOverflow: "ellipsis",
													whiteSpace: "nowrap",
													fontSize: 14,
													fontWeight: 600,
													color: "#e8e8e8",
												}}
											>
												{job.name}
											</span>
											<span
												style={{
													display: "block",
													marginTop: 4,
													overflow: "hidden",
													textOverflow: "ellipsis",
													whiteSpace: "nowrap",
													fontSize: 11,
													color: "#78a0b4",
												}}
											>
												{subtitle}
											</span>
										</span>
									</button>
									<span
										style={{
											fontSize: 11,
											fontWeight: 600,
											color: status.color,
											textAlign: "right",
										}}
									>
										{status.label}
									</span>
									<button
										type="button"
										onClick={() => void runNow(job)}
										aria-label={running ? "Stop " + job.name : "Run " + job.name}
										style={{
											width: 28,
											height: 28,
											border: 0,
											borderRadius: 6,
											background: "none",
											color: running ? "#888" : "#8b999d",
											display: "flex",
											alignItems: "center",
											justifyContent: "center",
											cursor: "pointer",
										}}
									>
										{running ? (
											<IconPlayerStop size={12} />
										) : (
											<IconPlayerPlay size={13} />
										)}
									</button>
									<button
										type="button"
										onClick={() =>
											router.push(
												`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(job.id)}`,
											)
										}
										aria-label={"Open " + job.name}
										style={{
											width: 24,
											height: 28,
											border: 0,
											background: "none",
											color: "#899699",
											display: "flex",
											alignItems: "center",
											justifyContent: "center",
											cursor: "pointer",
										}}
									>
										<IconChevronRight size={15} />
									</button>
								</div>
								{index < jobs.length - 1 && (
									<div style={{ height: 1, background: "#101010", margin: "0 40px" }} />
								)}
							</div>
						);
					})
				)}
				{!loading && !jobs.length && (
					<div style={{ padding: "28px 40px", fontSize: 13, color: "#666" }}>
						No scheduled tasks.
					</div>
				)}
			</section>
		</div>
	);
}
