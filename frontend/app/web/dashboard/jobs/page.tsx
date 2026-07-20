"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { IconChevronRight, IconClock, IconPlayerPlay, IconRefresh, IconSettings } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";

type Run = { id: string; state: string; message?: string | null; error?: string | null; createdAt: string; startedAt?: string | null; finishedAt?: string | null; progressCurrent: number; progressTotal: number };
type Job = { id: string; key: string; name: string; description?: string | null; kind: string; intervalMinutes: number; enabled: boolean; nextRunAt?: string | null; lastRunAt?: string | null; lastState: string; lastMessage?: string | null; recentRuns: Run[] };

const stateColor: Record<string, string> = { completed: "text-[#8fe4cf]", running: "text-sky-300", queued: "text-amber-300", failed: "text-red-300", error: "text-red-300", idle: "console-muted" };

export default function JobsPage() {
	const params = useSearchParams();
	const [session, setSession] = useState<Session | null>(null);
	const [jobs, setJobs] = useState<Job[]>([]);
	const [selectedId, setSelectedId] = useState(params.get("jobId") || "");
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [message, setMessage] = useState("");
	const selected = useMemo(() => jobs.find((job) => job.id === selectedId) || jobs[0], [jobs, selectedId]);

	async function load(current = session) {
		if (!current) return;
		setLoading(true);
		const response = await adminFetch("/api/admin/jobs", current);
		if (response.ok) {
			const value = await response.json();
			const next = value.jobs || [];
			setJobs(next);
			if (!selectedId && next[0]) setSelectedId(next[0].id);
		}
		setLoading(false);
	}

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) load(current);
	}, []);

	async function save() {
		if (!session || !selected) return;
		setSaving(true);
		const response = await adminFetch("/api/admin/jobs/" + selected.id, session, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ intervalMinutes: selected.intervalMinutes, enabled: selected.enabled }) });
		setMessage(response.ok ? "Task settings saved." : "Could not save task settings.");
		if (response.ok) load();
		setSaving(false);
	}

	async function runNow() {
		if (!session || !selected) return;
		const response = await adminFetch("/api/admin/jobs/" + selected.id + "/run", session, { method: "POST" });
		setMessage(response.ok ? "Task queued in the background." : "Could not queue task.");
		load();
	}

	return <div className="mx-auto max-w-6xl">
		<div className="flex flex-wrap items-end justify-between gap-4 border-b console-divider pb-6"><div><p className="console-kicker">Scheduler</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">Tasks</h1><p className="mt-2 text-sm console-muted">Background work runs independently so scans and metadata refreshes never block the dashboard.</p></div><button onClick={() => load()} className="rounded-lg border console-divider p-2.5 console-muted hover:bg-white/10" aria-label="Refresh tasks"><IconRefresh size={17} /></button></div>
		<div className="mt-7 grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
			<section className="console-card overflow-hidden rounded-xl"><div className="border-b console-divider px-5 py-4 text-xs uppercase tracking-[.16em] console-muted">All tasks <span className="ml-2 normal-case tracking-normal">{jobs.length}</span></div>{loading ? <div className="p-8 text-sm console-muted">Loading tasks…</div> : jobs.map((job) => <button key={job.id} onClick={() => setSelectedId(job.id)} className={"flex w-full items-center gap-4 border-b console-divider px-5 py-4 text-left transition last:border-0 hover:bg-white/[.035] " + (selected?.id === job.id ? "bg-white/[.055]" : "")}><span className={"rounded-full border p-2 " + (job.lastState === "failed" ? "border-red-400/30 text-red-300" : "border-white/10 text-[#8fe4cf]")}><IconClock size={16} /></span><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{job.name}</span><span className="mt-1 block truncate text-xs console-muted">{job.description || job.kind}</span></span><span className={"text-xs capitalize " + (stateColor[job.lastState] || "console-muted")}>{job.enabled ? job.lastState : "paused"}</span><IconChevronRight size={16} className="console-muted" /></button>)}{!loading && !jobs.length && <div className="p-8 text-sm console-muted">No scheduled tasks are configured.</div>}</section>
			{selected && <aside className="console-card rounded-xl p-5"><div className="flex items-start justify-between gap-3"><div><p className="console-kicker">Task settings</p><h2 className="mt-2 text-xl font-semibold">{selected.name}</h2></div><IconSettings className="console-muted" size={19} /></div><p className="mt-3 text-sm leading-6 console-muted">{selected.description}</p><label className="mt-7 block text-sm"><span className="console-muted">Run every (minutes)</span><input type="number" min={5} max={43200} value={selected.intervalMinutes} onChange={(event) => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, intervalMinutes: Number(event.target.value) } : job))} className="console-input mt-2 h-11 w-full rounded-lg px-3 outline-none" /></label><label className="mt-4 flex items-center justify-between text-sm"><span className="console-muted">Enabled</span><input type="checkbox" checked={selected.enabled} onChange={(event) => setJobs((current) => current.map((job) => job.id === selected.id ? { ...job, enabled: event.target.checked } : job))} /></label><div className="mt-6 grid grid-cols-2 gap-2"><button onClick={save} disabled={saving} className="console-button rounded-lg px-3 py-2.5 text-sm font-medium">{saving ? "Saving…" : "Save"}</button><button onClick={runNow} className="flex items-center justify-center gap-2 rounded-lg border console-divider px-3 py-2.5 text-sm console-muted hover:bg-white/10"><IconPlayerPlay size={15} />Run now</button></div>{message && <p className="mt-4 text-xs text-[#8fe4cf]">{message}</p>}<div className="mt-8 border-t console-divider pt-5"><p className="text-xs uppercase tracking-[.14em] console-muted">Recent runs</p><div className="mt-3 space-y-3">{selected.recentRuns?.slice(0, 6).map((run) => <div key={run.id} className="flex items-start justify-between gap-3 text-xs"><span><span className={"block capitalize " + (stateColor[run.state] || "console-muted")}>{run.state}</span><span className="mt-1 block console-muted">{run.message || run.error || "No details"}</span></span><time className="shrink-0 console-muted">{new Date(run.createdAt).toLocaleString()}</time></div>)}{!selected.recentRuns?.length && <p className="text-xs console-muted">No runs yet.</p>}</div></div></aside>}
		</div>
		<Link href="/web/dashboard" className="mt-7 inline-block text-xs console-muted hover:text-white">← Back to overview</Link>
	</div>;
}
