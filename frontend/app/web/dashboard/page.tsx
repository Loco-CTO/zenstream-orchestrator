"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { adminFetch, readSession, Session } from "./components/admin-client";

type Overview = {
	users: number;
	active_users: number;
	administrators: number;
	pending_invites: number;
};
type Task = { id: string; name: string; lastState: string; enabled: boolean };

export default function DashboardOverview() {
	const [session, setSession] = useState<Session | null>(null);
	const [overview, setOverview] = useState<Overview | null>(null);
	const [tasks, setTasks] = useState<Task[]>([]);
	const [error, setError] = useState("");

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (!current) return;
		Promise.all([
			adminFetch("/api/admin/overview", current),
			adminFetch("/api/admin/jobs", current),
		])
			.then(async ([overviewResponse, jobsResponse]) => {
				if (!overviewResponse.ok || !jobsResponse.ok)
					throw new Error("Dashboard data could not be loaded.");
				setOverview((await overviewResponse.json()) as Overview);
				setTasks(
					(((await jobsResponse.json()) as { jobs?: Task[] }).jobs || []).slice(
						0,
						5,
					),
				);
			})
			.catch((caught) =>
				setError(
					caught instanceof Error
						? caught.message
						: "Dashboard data could not be loaded.",
				),
			);
	}, []);

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
					{tasks.map((task, index) => (
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
											Last state {task.enabled ? task.lastState : "paused"}
										</div>
									</div>
								</div>
								<span style={{ color: "#444" }}>›</span>
							</Link>
							{index < tasks.length - 1 && (
								<div style={{ height: 1, background: "#111" }} />
							)}
						</div>
					))}
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
