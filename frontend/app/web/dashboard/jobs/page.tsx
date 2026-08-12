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
import { Job, activeStates } from "./job-types";

type Group = { name: string; jobs: Job[] };

function relativeTime(value?: string | null) {
	if (!value) return "never";
	const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
	if (seconds < 60) return "just now";
	if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
	if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
	return `${Math.floor(seconds / 86400)} days ago`;
}

function duration(job: Job) {
	const run = job.recentRuns?.[0];
	if (!run?.startedAt) return "< 1 min";
	const end = run.finishedAt ? new Date(run.finishedAt).getTime() : Date.now();
	const seconds = Math.max(0, (end - new Date(run.startedAt).getTime()) / 1000);
	return seconds < 60 ? "< 1 min" : `${Math.round(seconds / 60)} min`;
}

function groupFor(job: Job) {
	if (job.kind.includes("metadata")) return "Metadata";
	if (job.kind.includes("trickplay") || job.kind.includes("intro_outro"))
		return "Media Analysis";
	if (job.kind.includes("library")) return "Library";
	return "Catalog";
}

function Progress({ job }: { job: Job }) {
	const run = job.recentRuns?.find((item) => activeStates.has(item.state));
	if (!run || !run.progressTotal) return null;
	const value = Math.max(
		0,
		Math.min(100, (run.progressCurrent / run.progressTotal) * 100),
	);
	return (
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
						width: `${value}%`,
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
					fontFamily: '"DM Mono", monospace',
				}}
			>
				{Math.round(value)}%
			</div>
		</div>
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
	const groups = useMemo<Group[]>(() => {
		const order = ["Catalog", "Library", "Media Analysis", "Metadata"];
		return order
			.map((name) => ({
				name,
				jobs: jobs.filter((job) => groupFor(job) === name),
			}))
			.filter((group) => group.jobs.length);
	}, [jobs]);
	return (
		<div className="dashboard-page dashboard-design" style={{ maxWidth: 1080 }}>
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
			{loading ? (
				<div
					style={{
						background: "#080808",
						borderRadius: 12,
						padding: 22,
						color: "#666",
						fontSize: 13,
					}}
				>
					Loading tasks…
				</div>
			) : !groups.length ? (
				<div
					style={{
						background: "#080808",
						borderRadius: 12,
						padding: 22,
						color: "#666",
						fontSize: 13,
					}}
				>
					No scheduled tasks.
				</div>
			) : (
				<div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
					{groups.map((group) => (
						<section key={group.name}>
							<div
								style={{
									fontSize: 13,
									fontWeight: 600,
									color: "#fff",
									marginBottom: 10,
								}}
							>
								{group.name}
							</div>
							<div
								style={{ background: "#080808", borderRadius: 12, overflow: "hidden" }}
							>
								{group.jobs.map((job, index) => {
									const running = activeStates.has(job.lastState);
									return (
										<div key={job.id}>
											<div style={{ padding: "0 18px" }}>
												<div
													style={{
														display: "flex",
														alignItems: "center",
														gap: 14,
														paddingTop: 13,
														paddingBottom: running ? 8 : 13,
														cursor: "pointer",
													}}
													onClick={() =>
														router.push(
															`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(job.id)}`,
														)
													}
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
															{job.name}
														</div>
														<div style={{ fontSize: 11, color: "#555", marginTop: 2 }}>
															Last run {relativeTime(job.lastRunAt)} · {duration(job)}
														</div>
													</div>
													<span
														style={{
															width: 28,
															height: 28,
															borderRadius: 6,
															background: "none",
															color: "#444",
															display: "flex",
															alignItems: "center",
															justifyContent: "center",
														}}
													>
														{running ? (
															<IconPlayerStop size={12} />
														) : (
															<IconPlayerPlay size={12} />
														)}
													</span>
												</div>
												{running && <Progress job={job} />}
											</div>
											{index < group.jobs.length - 1 && (
												<div style={{ height: 1, background: "#111" }} />
											)}
										</div>
									);
								})}
							</div>
						</section>
					))}
				</div>
			)}
		</div>
	);
}
