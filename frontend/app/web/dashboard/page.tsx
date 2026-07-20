"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { IconArrowUpRight, IconClock, IconDatabase, IconLibrary, IconUsers } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "./components/admin-client";

type Overview = { users: number; active_users: number; disabled_users: number; administrators: number; pending_invites: number };
type Task = { id: string; name: string; lastState: string; enabled: boolean; nextRunAt?: string | null };

export default function DashboardOverview() {
	const [session, setSession] = useState<Session | null>(null);
	const [data, setData] = useState<Overview | null>(null);
	const [tasks, setTasks] = useState<Task[]>([]);
	useEffect(() => {
		const current = readSession();
		if (!current) return;
		setSession(current);
		adminFetch("/api/admin/overview", current).then((response) => response.ok && response.json()).then((value) => value && setData(value));
		adminFetch("/api/admin/jobs", current).then((response) => response.ok && response.json()).then((value) => value && setTasks((value.jobs || []).slice(0, 5)));
	}, []);
	const stats = data ? [["Users", data.users, "/web/dashboard/users", IconUsers], ["Active", data.active_users, "/web/dashboard/users", IconUsers], ["Administrators", data.administrators, "/web/dashboard/administrators", IconUsers], ["Pending invites", data.pending_invites, "/web/dashboard/invites", IconUsers]] as const : [];
	return <div className="mx-auto max-w-6xl">
		<div className="border-b console-divider pb-6"><p className="console-kicker">Overview</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">Dashboard</h1><p className="mt-2 text-sm console-muted">A quiet view of your server, libraries, and background work.</p></div>
		<section className="mt-7 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{stats.map(([label, value, href, Icon]) => <Link href={href} key={label} className="console-card rounded-xl p-4 transition hover:bg-white/[.045]"><div className="flex items-center justify-between"><span className="text-xs console-muted">{label}</span><Icon size={16} className="console-muted" /></div><p className="mt-4 text-2xl font-semibold">{value}</p></Link>)}</section>
		<div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
			<section className="console-card rounded-xl"><div className="flex items-center justify-between border-b console-divider px-5 py-4"><div><p className="console-kicker">Scheduler</p><h2 className="mt-1 text-lg font-semibold">Background tasks</h2></div><Link href="/web/dashboard/jobs" className="text-xs text-[#8fe4cf]">Manage all</Link></div>{tasks.map((task) => <Link href={"/web/dashboard/jobs?jobId=" + task.id} key={task.id} className="flex items-center gap-3 border-b console-divider px-5 py-4 last:border-0 hover:bg-white/[.035]"><IconClock size={17} className="text-[#8fe4cf]" /><span className="min-w-0 flex-1"><span className="block truncate text-sm">{task.name}</span><span className="mt-1 block text-xs console-muted">{task.enabled ? task.lastState : "paused"}{task.nextRunAt ? " · next " + new Date(task.nextRunAt).toLocaleString() : ""}</span></span><IconArrowUpRight size={16} className="console-muted" /></Link>)}{!tasks.length && <p className="px-5 py-8 text-sm console-muted">No scheduled work yet.</p>}</section>
			<section className="console-card rounded-xl p-5"><p className="console-kicker">Shortcuts</p><h2 className="mt-1 text-lg font-semibold">Configuration</h2><div className="mt-5 space-y-2"><Link href="/web/dashboard/libraries" className="flex items-center gap-3 rounded-lg border console-divider px-3 py-3 text-sm hover:bg-white/[.035]"><IconLibrary size={17} className="text-[#8fe4cf]" />Libraries<IconArrowUpRight size={15} className="ml-auto console-muted" /></Link><Link href="/web/dashboard/metadata" className="flex items-center gap-3 rounded-lg border console-divider px-3 py-3 text-sm hover:bg-white/[.035]"><IconDatabase size={17} className="text-[#8fe4cf]" />Metadata<IconArrowUpRight size={15} className="ml-auto console-muted" /></Link><Link href="/web/dashboard/profile" className="flex items-center gap-3 rounded-lg border console-divider px-3 py-3 text-sm hover:bg-white/[.035]"><IconUsers size={17} className="text-[#8fe4cf]" />Account security<IconArrowUpRight size={15} className="ml-auto console-muted" /></Link></div><p className="mt-6 border-t console-divider pt-4 text-xs console-muted">Signed in as {session?.username || "administrator"}</p></section>
		</div>
	</div>;
}
