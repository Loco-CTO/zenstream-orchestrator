"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { adminFetch, readSession, Session } from "./components/admin-client";
import {
	SessionCard,
	SessionCommand,
	SessionDetail,
	SessionDetailModal,
	type LiveSession,
} from "./components/session-viewer";
import { Run, activeStates, progressDetailText } from "./jobs/job-types";

type Overview = {
	users: number;
	active_users: number;
	administrators: number;
	pending_invites: number;
};
type Task = {
	id: string;
	name: string;
	lastState: string;
	lastMessage?: string | null;
	triggers?: unknown[];
	recentRuns?: Run[];
};

export default function DashboardOverview() {
	const [session, setSession] = useState<Session | null>(null);
	const [overview, setOverview] = useState<Overview | null>(null);
	const [tasks, setTasks] = useState<Task[]>([]);
	const [error, setError] = useState("");
	const [sessions, setSessions] = useState<LiveSession[]>([]);
	const [sessionError, setSessionError] = useState("");
	const [commandStates, setCommandStates] = useState<Record<string, string>>({});
	const [detail, setDetail] = useState<SessionDetail | null>(null);
	const [detailLoading, setDetailLoading] = useState(false);
	const [detailError, setDetailError] = useState("");

	const load = useCallback(async (current: Session) => {
		try {
			const [overviewResponse, jobsResponse] = await Promise.all([
				adminFetch("/api/admin/overview", current),
				adminFetch("/api/admin/jobs", current),
			]);
			if (!overviewResponse.ok || !jobsResponse.ok)
				throw new Error("Dashboard data could not be loaded.");
			setOverview((await overviewResponse.json()) as Overview);
			setTasks(
				(((await jobsResponse.json()) as { jobs?: Task[] }).jobs || []).slice(0, 5),
			);
			setError("");
		} catch (caught) {
			setError(
				caught instanceof Error
					? caught.message
					: "Dashboard data could not be loaded.",
			);
		}
	}, []);

	const loadSessions = useCallback(async (current: Session) => {
		try {
			const response = await adminFetch("/api/admin/sessions", current);
			if (!response.ok) throw new Error("Live sessions could not be loaded.");
			setSessions(
				((await response.json()) as { sessions?: LiveSession[] }).sessions || [],
			);
			setSessionError("");
		} catch (caught) {
			setSessionError(
				caught instanceof Error
					? caught.message
					: "Live sessions could not be loaded.",
			);
		}
	}, []);

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) void load(current);
	}, [load]);

	useEffect(() => {
		if (!session || !tasks.some((task) => activeStates.has(task.lastState)))
			return;
		const timer = window.setInterval(() => void load(session), 2000);
		return () => window.clearInterval(timer);
	}, [load, session, tasks]);

	useEffect(() => {
		if (!session) return;
		let active = true;
		let timer: number | undefined;
		const poll = async () => {
			if (!active) return;
			await loadSessions(session);
			if (active) timer = window.setTimeout(() => void poll(), 2_000);
		};
		void poll();
		return () => {
			active = false;
			if (timer !== undefined) window.clearTimeout(timer);
		};
	}, [loadSessions, session]);

	async function issueCommand(viewerId: string, command: SessionCommand) {
		if (!session) return;
		setCommandStates((current) => ({ ...current, [viewerId]: "Sending…" }));
		try {
			const response = await adminFetch(
				`/api/admin/sessions/${encodeURIComponent(viewerId)}/command`,
				session,
				{
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ action: command }),
				},
			);
			if (!response.ok) throw new Error("The session is no longer available.");
			setCommandStates((current) => ({ ...current, [viewerId]: "Pending" }));
		} catch (caught) {
			setCommandStates((current) => ({
				...current,
				[viewerId]: caught instanceof Error ? caught.message : "Command failed",
			}));
		}
	}

	async function openDetails(viewerId: string) {
		if (!session) return;
		setDetail(null);
		setDetailError("");
		setDetailLoading(true);
		try {
			const response = await adminFetch(
				`/api/admin/sessions/${encodeURIComponent(viewerId)}`,
				session,
			);
			if (response.status === 404)
				throw new Error("This playback session is no longer active.");
			if (!response.ok) throw new Error("Playback details could not be loaded.");
			setDetail((await response.json()) as SessionDetail);
		} catch (caught) {
			setDetailError(
				caught instanceof Error
					? caught.message
					: "Playback details could not be loaded.",
			);
		} finally {
			setDetailLoading(false);
		}
	}

	const stats = [
		["Users", overview?.users ?? "—", "/web/dashboard/users"],
		["Active", overview?.active_users ?? "—", "/web/dashboard/users"],
		[
			"Administrators",
			overview?.administrators ?? "—",
			"/web/dashboard/administrators",
		],
		[
			"Pending invites",
			overview?.pending_invites ?? "—",
			"/web/dashboard/invites",
		],
	] as const;
	return (
		<div className="dashboard-page dashboard-design">
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
						Dashboard
					</h1>
					<p
						style={{
							margin: "5px 0 0",
							fontSize: 13,
							color: "#666",
							lineHeight: 1.5,
						}}
					>
						A concise view of people, background work, and server configuration.
					</p>
				</div>
			</header>
			{error && (
				<p
					role="alert"
					style={{ color: "var(--danger)", fontSize: 12, marginBottom: 16 }}
				>
					{error}
				</p>
			)}
			<div
				style={{
					display: "grid",
					gridTemplateColumns: "repeat(4, 1fr)",
					gap: 12,
					marginBottom: 20,
				}}
			>
				{stats.map(([label, value, href]) => (
					<Link
						key={label}
						href={href}
						style={{
							display: "block",
							background: "#080808",
							borderRadius: 12,
							padding: "20px 22px",
							textDecoration: "none",
						}}
					>
						<div
							style={{
								fontSize: 11,
								fontWeight: 500,
								color: "#777",
								textTransform: "uppercase",
								letterSpacing: "0.07em",
								marginBottom: 10,
							}}
						>
							{label}
						</div>
						<div
							style={{
								fontSize: 30,
								fontWeight: 600,
								color: "#fff",
								letterSpacing: "-0.03em",
							}}
						>
							{value}
						</div>
					</Link>
				))}
			</div>
			<section style={{ marginBottom: 20 }}>
				<div
					style={{
						display: "flex",
						alignItems: "center",
						justifyContent: "space-between",
						marginBottom: 12,
					}}
				>
					<div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
						<h2 style={{ margin: 0, fontSize: 14, color: "#fff", fontWeight: 600 }}>
							Sessions
						</h2>
						<span
							style={{
								width: 20,
								height: 20,
								display: "grid",
								placeItems: "center",
								borderRadius: 10,
								background: "#111",
								color: "#777",
								fontSize: 10,
							}}
						>
							{sessions.length}
						</span>
					</div>
					<Link
						href="/web/dashboard/devices/"
						style={{ color: "#555", fontSize: 11, textDecoration: "none" }}
					>
						Devices →
					</Link>
				</div>
				{sessionError && (
					<p role="alert" style={{ color: "var(--danger)", fontSize: 12 }}>
						{sessionError}
					</p>
				)}
				<div
					style={{ display: "flex", gap: 10, overflowX: "auto", paddingBottom: 8 }}
				>
					{sessions.map((value) => (
						<SessionCard
							key={value.id}
							session={value}
							onCommand={(command) => void issueCommand(value.id, command)}
							onInfo={() => void openDetails(value.id)}
							commandState={commandStates[value.id]}
						/>
					))}
					{!sessions.length && !sessionError && (
						<div style={{ color: "#555", fontSize: 12, padding: "18px 0" }}>
							No active playback sessions.
						</div>
					)}
				</div>
			</section>
			<SessionDetailModal
				detail={detail}
				open={Boolean(detail || detailLoading || detailError)}
				loading={detailLoading}
				error={detailError}
				onClose={() => {
					setDetail(null);
					setDetailError("");
				}}
			/>
			<div style={{ display: "grid", gridTemplateColumns: "1fr 300px", gap: 12 }}>
				<section
					style={{ background: "#080808", borderRadius: 12, overflow: "hidden" }}
				>
					<div
						style={{
							padding: "18px 22px",
							display: "flex",
							alignItems: "center",
							justifyContent: "space-between",
						}}
					>
						<div>
							<span
								style={{
									fontSize: 10,
									fontWeight: 600,
									letterSpacing: "0.1em",
									textTransform: "uppercase",
									color: "var(--primary)",
									display: "block",
									marginBottom: 8,
								}}
							>
								Scheduler
							</span>
							<div style={{ fontSize: 15, fontWeight: 600, color: "#fff" }}>
								Background tasks
							</div>
						</div>
						<Link
							href="/web/dashboard/jobs/"
							style={{
								background: "#111",
								color: "#888",
								border: "1px solid #1f1f1f",
								borderRadius: 8,
								padding: "6px 14px",
								fontSize: 12,
								textDecoration: "none",
							}}
						>
							Manage all
						</Link>
					</div>
					<div style={{ height: 1, background: "#111" }} />
					{tasks.map((task, index) => {
						const activeRun = task.recentRuns?.find((run) =>
							activeStates.has(run.state),
						);
						const detail = progressDetailText(activeRun?.progressDetail);
						return (
							<div key={task.id}>
								<Link
									href={`/web/dashboard/jobs/detail/?jobId=${encodeURIComponent(task.id)}`}
									style={{
										display: "flex",
										alignItems: "center",
										justifyContent: "space-between",
										padding: "13px 22px",
										textDecoration: "none",
									}}
								>
									<div style={{ display: "flex", alignItems: "center", gap: 12 }}>
										<span
											style={{
												color: task.lastState === "running" ? "var(--primary)" : "#666",
												fontSize: 14,
											}}
										>
											◷
										</span>
										<div>
											<div style={{ fontSize: 13, fontWeight: 500, color: "#ccc" }}>
												{task.name}
											</div>
											<div style={{ fontSize: 11, color: "#777", marginTop: 2 }}>
												{activeRun
													? activeRun.message || activeRun.state
													: `Last state ${task.triggers?.length ? task.lastState : "paused"}`}
											</div>
											{activeRun && detail && detail !== activeRun.message && (
												<div
													style={{
														fontSize: 10,
														color: "#666",
														marginTop: 2,
														fontFamily: "var(--font-mono)",
													}}
												>
													{detail}
												</div>
											)}
										</div>
									</div>
									<span style={{ color: "#444" }}>›</span>
								</Link>
								{index < tasks.length - 1 && (
									<div style={{ height: 1, background: "#111" }} />
								)}
							</div>
						);
					})}
					{!tasks.length && (
						<div style={{ padding: "24px 22px", color: "#666", fontSize: 13 }}>
							No scheduled tasks.
						</div>
					)}
				</section>
				<section
					style={{ background: "#080808", borderRadius: 12, padding: "18px 22px" }}
				>
					<span
						style={{
							fontSize: 10,
							fontWeight: 600,
							letterSpacing: "0.1em",
							textTransform: "uppercase",
							color: "var(--primary)",
							display: "block",
							marginBottom: 8,
						}}
					>
						Shortcuts
					</span>
					<div
						style={{ fontSize: 15, fontWeight: 600, color: "#fff", marginBottom: 16 }}
					>
						Configuration
					</div>
					<div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
						{[
							["Libraries", "/web/dashboard/libraries/"],
							["Metadata", "/web/dashboard/metadata/"],
							["Account security", "/web/dashboard/profile/"],
						].map(([label, href]) => (
							<Link
								key={label}
								href={href}
								style={{
									display: "flex",
									alignItems: "center",
									justifyContent: "space-between",
									padding: "11px 14px",
									borderRadius: 8,
									background: "#0d0d0d",
									color: "#888",
									textDecoration: "none",
									fontSize: 13,
								}}
							>
								{label}
								<span>›</span>
							</Link>
						))}
					</div>
					<p
						style={{
							marginTop: 24,
							paddingTop: 14,
							borderTop: "1px solid #111",
							color: "#444",
							fontSize: 11,
						}}
					>
						Signed in as {session?.username || "administrator"}
					</p>
				</section>
			</div>
		</div>
	);
}
