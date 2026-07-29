"use client";

import { useEffect, useState } from "react";
import { IconPlayerPlay, IconRefresh } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import { PageHeader, StatusMessage, SurfaceCard } from "../components/dashboard-surface";

type Task = {
	id: string;
	enabled: boolean;
	intervalMinutes: number;
	lastState?: string;
	lastMessage?: string | null;
	nextRunAt?: string | null;
};

export default function IntroOutroPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [scanOnAdded, setScanOnAdded] = useState(true);
	const [task, setTask] = useState<Task | null>(null);
	const [saving, setSaving] = useState(false);
	const [message, setMessage] = useState("");

	async function load(current = session) {
		if (!current) return;
		const response = await adminFetch("/api/admin/intro-outro/settings", current);
		if (!response.ok) return;
		const value = await response.json();
		setScanOnAdded(Boolean(value.scanOnAdded));
		setTask(value.task || null);
	}

	useEffect(() => {
		const current = readSession();
		setSession(current);
		void load(current);
	}, []);

	async function save() {
		if (!session) return;
		setSaving(true);
		const response = await adminFetch("/api/admin/intro-outro/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ scanOnAdded }),
		});
		setMessage(response.ok ? "Intro & outro settings saved." : "Could not save settings.");
		setSaving(false);
	}

	async function runNow() {
		if (!session || !task) return;
		const response = await adminFetch(`/api/admin/jobs/${task.id}/run`, session, { method: "POST" });
		setMessage(response.ok ? "Detection task queued." : "Could not queue detection.");
		void load();
	}

	return (
		<div className="max-w-4xl">
			<PageHeader
				title="Intro & Outro"
				description="Detect recurring episode intros and outros from locally generated audio fingerprints."
				actions={<button onClick={() => void load()} className="material-icon-button" aria-label="Refresh"><IconRefresh size={17} /></button>}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			<SurfaceCard className="mt-7 p-6">
				<label className="flex items-start justify-between gap-6 text-sm">
					<span><span className="block font-medium text-white">Scan when media is added</span><span className="mt-1 block leading-6 console-muted">Queue new or changed TV episodes after their library scan completes, without waiting for the next task cycle.</span></span>
					<input type="checkbox" checked={scanOnAdded} onChange={(event) => setScanOnAdded(event.target.checked)} />
				</label>
				<div className="mt-6 flex gap-2">
					<button onClick={() => void save()} disabled={saving} className="console-button rounded-lg px-4 py-2.5 text-sm disabled:opacity-50">{saving ? "Saving…" : "Save settings"}</button>
					<button onClick={() => void runNow()} disabled={!task} className="flex items-center gap-2 rounded-lg border console-divider px-4 py-2.5 text-sm console-muted hover:bg-white/10 disabled:opacity-50"><IconPlayerPlay size={16} />Run now</button>
				</div>
			</SurfaceCard>
			<SurfaceCard className="mt-5 p-6">
				<p className="console-kicker">Detection task</p>
				{task ? <div className="mt-3 space-y-2 text-sm"><p><span className="console-muted">Status: </span>{task.enabled ? task.lastState || "idle" : "paused"}</p><p><span className="console-muted">Cadence: </span>every {task.intervalMinutes} minutes</p>{task.lastMessage && <p className="console-muted">{task.lastMessage}</p>}{task.nextRunAt && <p className="console-muted">Next run {new Date(task.nextRunAt).toLocaleString()}</p>}</div> : <p className="mt-3 text-sm console-muted">The task will appear after the Orchestrator starts with the latest schema.</p>}
			</SurfaceCard>
		</div>
	);
}
