"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
	IconArrowLeft,
	IconClock,
	IconMinus,
	IconPlayerPlay,
	IconPlayerStop,
	IconPlus,
	IconRefresh,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import { Job, JobTrigger, activeStates, stateColor } from "./job-types";

const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function triggerLabel(trigger: JobTrigger) {
	if (trigger.type === "startup") return "On application start";
	if (trigger.type === "daily") return `Daily at ${trigger.time}`;
	if (trigger.type === "weekly") return `Weekly on ${days[trigger.weekday]} at ${trigger.time}`;
	const seconds = trigger.intervalSeconds;
	if (seconds % 3600 === 0) return `Every ${seconds / 3600} hours`;
	if (seconds % 60 === 0) return `Every ${seconds / 60} minutes`;
	return `Every ${seconds} seconds`;
}

const fieldStyle: React.CSSProperties = {
	width: "100%",
	background: "#1a1a1a",
	border: "1px solid var(--border-strong)",
	borderRadius: 8,
	padding: "10px 14px",
	color: "var(--text)",
	fontSize: 14,
	fontFamily: "var(--font-sans)",
};

function Btn({ children, onClick, variant = "primary", icon }: { children: React.ReactNode; onClick?: () => void; variant?: "primary" | "ghost"; icon?: React.ReactNode }) {
	return (
		<button type="button" onClick={onClick} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, border: variant === "ghost" ? "1px solid var(--border-strong)" : "none", background: variant === "ghost" ? "transparent" : "var(--primary)", color: variant === "ghost" ? "#aaa" : "#000", borderRadius: 7, padding: "9px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "var(--font-sans)" }}>
			{icon}{children}
		</button>
	);
}

function Modal({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode }) {
	if (!open) return null;
	return (
		<div role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()} style={{ position: "fixed", inset: 0, zIndex: 50, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,.72)", padding: 20 }}>
			<div role="dialog" aria-modal="true" aria-label={title} style={{ width: "100%", maxWidth: 420, background: "#101010", border: "1px solid var(--border-strong)", borderRadius: 12, padding: 22, boxShadow: "0 24px 80px rgba(0,0,0,.5)" }}>
				<div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}><h2 style={{ margin: 0, fontSize: 16, color: "#fff", fontWeight: 600 }}>{title}</h2><button type="button" onClick={onClose} aria-label="Close" style={{ border: 0, background: "none", color: "#666", cursor: "pointer", fontSize: 20 }}>×</button></div>
				{children}
			</div>
		</div>
	);
}

export default function JobDetailPage() {
	const params = useSearchParams();
	const requestedId = params.get("jobId") || "";
	const [session, setSession] = useState<Session | null>(null);
	const [jobs, setJobs] = useState<Job[]>([]);
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [message, setMessage] = useState("");
	const [addingTrigger, setAddingTrigger] = useState(false);
	const [triggerType, setTriggerType] = useState<JobTrigger["type"]>("daily");
	const [triggerTime, setTriggerTime] = useState("00:00");
	const [triggerDay, setTriggerDay] = useState("0");
	const [triggerIntervalVal, setTriggerIntervalVal] = useState("15");
	const [triggerIntervalUnit, setTriggerIntervalUnit] = useState("minutes");
	const selected = useMemo(() => jobs.find((job) => job.id === requestedId), [jobs, requestedId]);
	const activeRun = selected?.recentRuns?.find((run) => activeStates.has(run.state));

	const load = useCallback(async (current: Session, showLoading = true) => {
		if (showLoading) setLoading(true);
		const response = await adminFetch("/api/admin/jobs", current);
		if (response.ok) setJobs(((await response.json()) as { jobs?: Job[] }).jobs || []);
		if (showLoading) setLoading(false);
	}, []);

	useEffect(() => { const current = readSession(); setSession(current); if (current) void load(current); }, [load]);
	useEffect(() => { if (!session || !selected?.recentRuns?.some((run) => activeStates.has(run.state))) return; const timer = window.setInterval(() => void load(session, false), 2000); return () => window.clearInterval(timer); }, [load, selected, session]);

	function updateSelected(change: (job: Job) => Job) { setJobs((current) => current.map((job) => job.id === requestedId ? change(job) : job)); }

	async function save() {
		if (!session || !selected) return;
		setSaving(true);
		const response = await adminFetch(`/api/admin/jobs/${selected.id}`, session, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ intervalMinutes: selected.intervalMinutes, enabled: selected.enabled, config: selected.config || {}, triggers: selected.triggers || [] }) });
		setMessage(response.ok ? "Task settings saved." : "Could not save task settings.");
		if (response.ok) await load(session, false);
		setSaving(false);
	}
	async function runNow() { if (!session || !selected) return; const response = await adminFetch(`/api/admin/jobs/${selected.id}/run`, session, { method: "POST" }); setMessage(response.ok ? "Task queued in the background." : "Could not queue task."); await load(session, false); }
	async function terminate() { if (!session || !selected || !activeRun) return; await adminFetch(`/api/admin/jobs/${selected.id}/runs/${activeRun.id}/terminate`, session, { method: "POST" }); await load(session, false); }

	function addTrigger() {
		const id = crypto.randomUUID();
		const trigger: JobTrigger = triggerType === "startup" ? { id, type: "startup" } : triggerType === "daily" ? { id, type: "daily", time: triggerTime } : triggerType === "weekly" ? { id, type: "weekly", weekday: Number(triggerDay), time: triggerTime } : { id, type: "interval", intervalSeconds: Math.min(2592000, Math.max(1, Number(triggerIntervalVal) * (triggerIntervalUnit === "hours" ? 3600 : triggerIntervalUnit === "minutes" ? 60 : 1))) };
		updateSelected((job) => ({ ...job, triggers: [...(job.triggers || []), trigger] }));
		setAddingTrigger(false);
	}

	if (!requestedId || (!loading && !selected)) return <div className="dashboard-page dashboard-design"><Link href="/web/dashboard/jobs" style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "#666", fontSize: 13, textDecoration: "none" }}><IconArrowLeft size={14} /> Back to tasks</Link><h1 style={{ margin: "22px 0 6px", fontSize: 20, color: "#fff" }}>{requestedId ? "Task not found" : "Task not selected"}</h1><p style={{ margin: 0, color: "#666", fontSize: 13 }}>Choose a scheduled task to inspect its settings and recent runs.</p></div>;
	if (!selected) return <div className="dashboard-page dashboard-design" style={{ color: "#666", fontSize: 13 }}>Loading task…</div>;

	return <div className="dashboard-page dashboard-design">
		<div style={{ marginBottom: 22 }}><Link href="/web/dashboard/jobs" style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "none", border: "none", color: "#666", cursor: "pointer", fontSize: 13, textDecoration: "none" }}><IconArrowLeft size={14} /> Back to tasks</Link></div>
		<div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 6, gap: 16 }}><div><h1 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: "#fff", letterSpacing: "-0.02em" }}>{selected.name}</h1><p style={{ margin: "6px 0 0", fontSize: 13, color: "#666", lineHeight: 1.55 }}>{selected.description || "Review scheduler settings, triggers, and recent runs."}</p></div><button type="button" onClick={() => session && void load(session)} aria-label="Refresh task" style={{ width: 32, height: 32, border: 0, background: "none", color: "#777", cursor: "pointer" }}><IconRefresh size={15} /></button></div>
		<div style={{ marginBottom: 16 }}><Btn icon={<IconPlus size={14} />} onClick={() => setAddingTrigger(true)}>Add trigger</Btn></div>
		{message && <div role="status" style={{ color: "var(--primary)", fontSize: 12, marginBottom: 12 }}>{message}</div>}
		<Modal open={addingTrigger} onClose={() => setAddingTrigger(false)} title="Add trigger"><div style={{ display: "flex", flexDirection: "column", gap: 14 }}><label style={{ fontSize: 10, fontWeight: 600, letterSpacing: ".1em", textTransform: "uppercase", color: "var(--primary)" }}>Trigger type<select value={triggerType} onChange={(event) => setTriggerType(event.target.value as JobTrigger["type"])} style={{ ...fieldStyle, marginTop: 8, appearance: "none" }}><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="interval">Interval</option><option value="startup">On application start</option></select></label>{triggerType === "weekly" && <label style={{ fontSize: 10, fontWeight: 600, letterSpacing: ".1em", textTransform: "uppercase", color: "var(--primary)" }}>Day<select value={triggerDay} onChange={(event) => setTriggerDay(event.target.value)} style={{ ...fieldStyle, marginTop: 8 }}>{days.map((day, index) => <option key={day} value={index}>{day}</option>)}</select></label>}{(triggerType === "daily" || triggerType === "weekly") && <label style={{ fontSize: 10, fontWeight: 600, letterSpacing: ".1em", textTransform: "uppercase", color: "var(--primary)" }}>Time<input type="time" value={triggerTime} onChange={(event) => setTriggerTime(event.target.value)} style={{ ...fieldStyle, marginTop: 8 }} /></label>}{triggerType === "interval" && <div><label style={{ fontSize: 10, fontWeight: 600, letterSpacing: ".1em", textTransform: "uppercase", color: "var(--primary)" }}>Every</label><div style={{ display: "flex", gap: 8, marginTop: 8 }}><input type="number" min={1} value={triggerIntervalVal} onChange={(event) => setTriggerIntervalVal(event.target.value)} style={{ ...fieldStyle, flex: 1 }} /><select value={triggerIntervalUnit} onChange={(event) => setTriggerIntervalUnit(event.target.value)} style={{ ...fieldStyle, flex: 1 }}><option>seconds</option><option>minutes</option><option>hours</option></select></div></div>}<div style={{ display: "flex", justifyContent: "flex-end", gap: 8, paddingTop: 4 }}><Btn variant="ghost" onClick={() => setAddingTrigger(false)}>Cancel</Btn><Btn onClick={addTrigger}>Add</Btn></div></div></Modal>
		<div style={{ background: "#080808", borderRadius: 12, padding: 0 }}><div style={{ padding: "11px 18px", background: "#0d0d0d", borderRadius: "12px 12px 0 0" }}><span style={{ fontSize: 11, fontWeight: 600, color: "#555", letterSpacing: ".08em", textTransform: "uppercase" }}>Schedule</span></div><div style={{ height: 1, background: "#111" }} />{(selected.triggers || []).length === 0 ? <div style={{ padding: "20px 18px", fontSize: 13, color: "#333" }}>No triggers configured. Add one above.</div> : selected.triggers.map((trigger, index) => <div key={trigger.id}><div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 18px" }}><span style={{ fontSize: 14, color: "#ccc", fontWeight: 500 }}>{triggerLabel(trigger)}</span><button type="button" aria-label="Remove trigger" onClick={() => updateSelected((job) => ({ ...job, triggers: job.triggers.filter((entry) => entry.id !== trigger.id) }))} style={{ width: 22, height: 22, borderRadius: "50%", background: "var(--danger)", border: "none", color: "#000", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}><IconMinus size={10} stroke={3} /></button></div>{index < selected.triggers.length - 1 && <div style={{ height: 1, background: "#111" }} />}</div>)}</div>
		<div style={{ display: "flex", alignItems: "center", gap: 18, marginTop: 16, padding: "12px 2px", color: "#777", fontSize: 12 }}><label style={{ display: "flex", alignItems: "center", gap: 8 }}>Interval (minutes)<input type="number" min={1} max={43200} value={selected.intervalMinutes} onChange={(event) => updateSelected((job) => ({ ...job, intervalMinutes: Number(event.target.value) }))} style={{ ...fieldStyle, width: 96, padding: "7px 9px", fontSize: 12 }} /></label><label style={{ display: "flex", alignItems: "center", gap: 8 }}><input type="checkbox" checked={selected.enabled} onChange={(event) => updateSelected((job) => ({ ...job, enabled: event.target.checked }))} /> Enabled</label>{selected.kind === "metadata_refresh" && <label style={{ display: "flex", alignItems: "center", gap: 8 }}><input type="checkbox" checked={Boolean(selected.config?.preserveCachedAssets)} onChange={(event) => updateSelected((job) => ({ ...job, config: { ...(job.config || {}), preserveCachedAssets: event.target.checked } }))} /> Preserve cached assets</label>}</div>
		<div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap" }}><Btn onClick={() => void save()}>{saving ? "Saving…" : "Save settings"}</Btn><Btn variant="ghost" icon={<IconPlayerPlay size={14} />} onClick={() => void runNow()}>{activeRun ? "Task active" : "Run now"}</Btn>{activeRun && <Btn variant="ghost" icon={<IconPlayerStop size={14} />} onClick={() => void terminate()}>Terminate active run</Btn>}</div>
		<div style={{ marginTop: 28, background: "#080808", borderRadius: 12, padding: "20px 22px" }}><div style={{ display: "flex", alignItems: "center", gap: 8 }}><IconClock size={16} color="var(--primary)" /><span style={{ fontSize: 13, color: stateColor[selected.lastState] ? undefined : "#666" }}>{selected.enabled ? selected.lastState : "paused"}</span></div>{selected.recentRuns?.slice(0, 8).map((run) => <div key={run.id} style={{ display: "flex", justifyContent: "space-between", gap: 12, marginTop: 14, fontSize: 12, color: "#666" }}><span>{run.message || run.error || run.state}</span><time>{new Date(run.createdAt).toLocaleString()}</time></div>)}</div>
	</div>;
}
