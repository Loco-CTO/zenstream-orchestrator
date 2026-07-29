"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
	IconArrowUpRight,
	IconClock,
	IconDatabase,
	IconLibrary,
	IconRefresh,
	IconUsers,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "./components/admin-client";
import { PageHeader, SectionHeader, SurfaceCard } from "./components/dashboard-surface";

type Overview = {
	users: number;
	active_users: number;
	disabled_users: number;
	administrators: number;
	pending_invites: number;
};
type Task = {
	id: string;
	name: string;
	lastState: string;
	enabled: boolean;
	nextRunAt?: string | null;
};

export default function DashboardOverview() {
	const [session, setSession] = useState<Session | null>(null);
	const [data, setData] = useState<Overview | null>(null);
	const [tasks, setTasks] = useState<Task[]>([]);
	async function load(current = session) {
		if (!current) return;
		const [overviewResponse, jobsResponse] = await Promise.all([
			adminFetch("/api/admin/overview", current),
			adminFetch("/api/admin/jobs", current),
		]);
		if (overviewResponse.ok) setData(await overviewResponse.json());
		if (jobsResponse.ok)
			setTasks(((await jobsResponse.json()).jobs || []).slice(0, 5));
	}
	useEffect(() => {
		const current = readSession();
		if (!current) return;
		setSession(current);
		load(current);
	}, []);
	const stats = data
		? ([
				["Users", data.users, "/web/dashboard/users", IconUsers],
				["Active", data.active_users, "/web/dashboard/users", IconUsers],
				[
					"Administrators",
					data.administrators,
					"/web/dashboard/administrators",
					IconUsers,
				],
				[
					"Pending invites",
					data.pending_invites,
					"/web/dashboard/invites",
					IconUsers,
				],
			] as const)
		: [];
	return (
		<div className="max-w-6xl">
			<PageHeader
				title="Dashboard"
				description="A concise view of people, background work, and server configuration."
				actions={
				<button
					onClick={() => {
						const current = readSession();
						if (current) {
							setSession(current);
							load(current);
						}
					}}
					className="material-icon-button"
					aria-label="Refresh dashboard"
					title="Refresh dashboard"
				>
					<IconRefresh size={17} />
				</button>
				}
			/>
			<section className="mt-7 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
				{stats.map(([label, value, href, Icon]) => (
					<Link
						href={href}
						key={label}
						className="console-card rounded-xl p-4 transition hover:bg-white/[.045]"
					>
						<div className="flex items-center justify-between">
							<span className="text-xs console-muted">{label}</span>
							<Icon size={16} className="console-muted" />
						</div>
						<p className="mt-4 text-2xl font-semibold">{value}</p>
					</Link>
				))}
			</section>
			<div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
				<SurfaceCard>
					<SectionHeader kicker="Scheduler" title="Background tasks" action={<Link href="/web/dashboard/jobs" className="text-xs text-[#aeb9ff]">
							Manage all
						</Link>} />
					{tasks.map((task) => (
						<Link
							href={"/web/dashboard/jobs?jobId=" + task.id}
							key={task.id}
							className="flex items-center gap-3 border-b console-divider px-5 py-4 last:border-0 hover:bg-white/[.035]"
						>
							<IconClock size={17} className="text-[#aeb9ff]" />
							<span className="min-w-0 flex-1">
								<span className="block truncate text-sm">{task.name}</span>
								<span className="mt-1 block text-xs console-muted">
									{task.enabled ? task.lastState : "paused"}
									{task.nextRunAt
										? " · next " + new Date(task.nextRunAt).toLocaleString()
										: ""}
								</span>
							</span>
							<IconArrowUpRight size={16} className="console-muted" />
						</Link>
					))}
					{!tasks.length && (
						<p className="px-5 py-8 text-sm console-muted">
							No scheduled work yet.
						</p>
					)}
				</SurfaceCard>
				<SurfaceCard className="p-5">
					<p className="console-kicker">Shortcuts</p>
					<h2 className="mt-1 text-lg font-semibold">Configuration</h2>
					<div className="mt-5 space-y-2">
						<Link
							href="/web/dashboard/libraries"
							className="flex items-center gap-3 rounded-lg border console-divider px-3 py-3 text-sm hover:bg-white/[.035]"
						>
							<IconLibrary size={17} className="text-[#aeb9ff]" />
							Libraries
							<IconArrowUpRight size={15} className="ml-auto console-muted" />
						</Link>
						<Link
							href="/web/dashboard/metadata"
							className="flex items-center gap-3 rounded-lg border console-divider px-3 py-3 text-sm hover:bg-white/[.035]"
						>
							<IconDatabase size={17} className="text-[#aeb9ff]" />
							Metadata
							<IconArrowUpRight size={15} className="ml-auto console-muted" />
						</Link>
						<Link
							href="/web/dashboard/profile"
							className="flex items-center gap-3 rounded-lg border console-divider px-3 py-3 text-sm hover:bg-white/[.035]"
						>
							<IconUsers size={17} className="text-[#aeb9ff]" />
							Account security
							<IconArrowUpRight size={15} className="ml-auto console-muted" />
						</Link>
					</div>
					<p className="mt-6 border-t console-divider pt-4 text-xs console-muted">
						Signed in as {session?.username || "administrator"}
					</p>
				</SurfaceCard>
			</div>
		</div>
	);
}
