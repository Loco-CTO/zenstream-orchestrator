"use client";

import { useCallback, useEffect, useState } from "react";
import { IconPlayerPlay, IconRefresh, IconTrash } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import { activeStates } from "../jobs/job-types";
import {
	ConfirmDialog,
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type Task = {
	id: string;
	triggers?: { type: string; intervalSeconds?: number; time?: string }[];
	lastState?: string;
	lastMessage?: string | null;
	nextRunAt?: string | null;
};
type Settings = {
	scanOnAdded: boolean;
	analysisPercent: number;
	analysisLengthLimitMinutes: number;
	scanIntroduction: boolean;
	scanCredits: boolean;
	minimumIntroDuration: number;
	maximumIntroDuration: number;
	minimumCreditsDuration: number;
	maximumCreditsAnalysisSeconds: number;
	maximumFingerprintPointDifferences: number;
	maximumTimeSkipSeconds: number;
	invertedIndexShift: number;
	introOutroWorkers: number;
};

const defaults: Settings = {
	scanOnAdded: true,
	analysisPercent: 25,
	analysisLengthLimitMinutes: 10,
	scanIntroduction: true,
	scanCredits: true,
	minimumIntroDuration: 15,
	maximumIntroDuration: 120,
	minimumCreditsDuration: 15,
	maximumCreditsAnalysisSeconds: 450,
	maximumFingerprintPointDifferences: 6,
	maximumTimeSkipSeconds: 3.5,
	invertedIndexShift: 2,
	introOutroWorkers: 1,
};

function NumberField({
	label,
	hint,
	value,
	onChange,
	step = 1,
	minimum = 0,
	maximum,
}: {
	label: string;
	hint: string;
	value: number;
	onChange: (value: number) => void;
	step?: number;
	minimum?: number;
	maximum?: number;
}) {
	return (
		<label className="block text-sm">
			<span className="font-medium text-white">{label}</span>
			<span className="mt-1 block text-xs leading-5 console-muted">{hint}</span>
			<input
				className="mt-2 w-full rounded-lg border console-divider bg-black/20 px-3 py-2 text-sm"
				type="number"
				min={minimum}
				max={maximum}
				step={step}
				value={value}
				onChange={(event) => onChange(Number(event.target.value))}
			/>
		</label>
	);
}

export default function IntroOutroPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [settings, setSettings] = useState<Settings>(defaults);
	const [task, setTask] = useState<Task | null>(null);
	const [saving, setSaving] = useState(false);
	const [clearing, setClearing] = useState(false);
	const [confirmClear, setConfirmClear] = useState(false);
	const [message, setMessage] = useState("");

	const update = <K extends keyof Settings>(key: K, value: Settings[K]) =>
		setSettings((current) => ({ ...current, [key]: value }));
	const load = useCallback(
		async (current: Session | null) => {
			if (!current) return;
			const response = await adminFetch(
				"/api/admin/intro-outro/settings",
				current,
			);
			if (!response.ok) return;
			const value = await response.json();
			setSettings({ ...defaults, ...value });
			setTask(value.task || null);
		},
		[],
	);
	useEffect(() => {
		const current = readSession();
		setSession(current);
		void load(current);
	}, [load]);
	useEffect(() => {
		if (!session || !task?.lastState || !activeStates.has(task.lastState)) return;
		const timer = window.setInterval(() => void load(session), 2000);
		return () => window.clearInterval(timer);
	}, [load, session, task?.lastState]);
	async function save() {
		if (!session) return;
		setSaving(true);
		const response = await adminFetch(
			"/api/admin/intro-outro/settings",
			session,
			{
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify(settings),
			},
		);
		setMessage(
			response.ok
				? "Intro & outro settings saved; detection was queued to apply them."
				: "Could not save settings.",
		);
		setSaving(false);
		if (response.ok) void load(session);
	}
	async function runNow() {
		if (!session || !task) return;
		const response = await adminFetch(`/api/admin/jobs/${task.id}/run`, session, {
			method: "POST",
		});
		setMessage(
			response.ok ? "Detection task queued." : "Could not queue detection.",
		);
		void load(session);
	}
	async function clearDetected() {
		if (!session) return;
		setClearing(true);
		const response = await adminFetch("/api/admin/intro-outro/clear", session, {
			method: "POST",
		});
		const value = response.ok ? await response.json() : null;
		setMessage(
			response.ok
				? `Removed ${value.removedSegments} detected intro/outro ranges. Cached fingerprints were kept.`
				: "Could not remove detected ranges.",
		);
		setClearing(false);
		setConfirmClear(false);
	}

	return (
		<div className="max-w-5xl">
			<ConfirmDialog
				open={confirmClear}
				title="Remove every detected intro and outro?"
				description="This removes all skip ranges immediately. Cached fingerprints stay available for a later detection run."
				confirmLabel="Remove all ranges"
				destructive
				busy={clearing}
				onClose={() => setConfirmClear(false)}
				onConfirm={() => void clearDetected()}
			/>
			<PageHeader
				title="Intro & Outro"
				description="Compare raw Chromaprint audio points across episodes. Each episode keeps its own matching timestamps."
				actions={
					<button
						onClick={() => void load(session)}
						className="material-icon-button"
						aria-label="Refresh"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			<SurfaceCard className="mt-7 p-6">
				<div className="grid gap-5 md:grid-cols-2">
					<label className="flex items-start gap-3 text-sm">
						<input
							type="checkbox"
							checked={settings.scanOnAdded}
							onChange={(event) => update("scanOnAdded", event.target.checked)}
						/>
						<span>
							<span className="font-medium text-white">Scan when media is added</span>
							<span className="mt-1 block leading-6 console-muted">
								Queue new or changed numbered TV episodes as soon as a library scan
								completes.
							</span>
						</span>
					</label>
					<label className="flex items-start gap-3 text-sm">
						<input
							type="checkbox"
							checked={settings.scanIntroduction}
							onChange={(event) => update("scanIntroduction", event.target.checked)}
						/>
						<span>
							<span className="font-medium text-white">Scan introductions</span>
							<span className="mt-1 block leading-6 console-muted">
								Fingerprint the opening window and detect recurring intros.
							</span>
						</span>
					</label>
					<label className="flex items-start gap-3 text-sm">
						<input
							type="checkbox"
							checked={settings.scanCredits}
							onChange={(event) => update("scanCredits", event.target.checked)}
						/>
						<span>
							<span className="font-medium text-white">Scan credits / outros</span>
							<span className="mt-1 block leading-6 console-muted">
								Fingerprint the tail window and detect recurring credits.
							</span>
						</span>
					</label>
				</div>
				<div className="mt-7 grid gap-5 md:grid-cols-2 lg:grid-cols-3">
					<NumberField
						label="Detection workers"
						hint="Maximum concurrent FFmpeg fingerprint processes. Use 1–64 workers."
						value={settings.introOutroWorkers}
						minimum={1}
						maximum={64}
						onChange={(value) => update("introOutroWorkers", value)}
					/>
					<NumberField
						label="Opening analysis (%)"
						hint="Episode percentage analysed from the beginning."
						value={settings.analysisPercent}
						onChange={(value) => update("analysisPercent", value)}
					/>
					<NumberField
						label="Opening analysis limit (minutes)"
						hint="Upper bound for each opening fingerprint."
						value={settings.analysisLengthLimitMinutes}
						onChange={(value) => update("analysisLengthLimitMinutes", value)}
					/>
					<NumberField
						label="Minimum intro (seconds)"
						hint="Ignore shorter opening matches."
						value={settings.minimumIntroDuration}
						onChange={(value) => update("minimumIntroDuration", value)}
					/>
					<NumberField
						label="Maximum intro (seconds)"
						hint="Ignore longer opening matches."
						value={settings.maximumIntroDuration}
						onChange={(value) => update("maximumIntroDuration", value)}
					/>
					<NumberField
						label="Minimum credits (seconds)"
						hint="Ignore shorter tail matches."
						value={settings.minimumCreditsDuration}
						onChange={(value) => update("minimumCreditsDuration", value)}
					/>
					<NumberField
						label="Credits analysis window (seconds)"
						hint="Tail length fingerprinted for each episode."
						value={settings.maximumCreditsAnalysisSeconds}
						onChange={(value) => update("maximumCreditsAnalysisSeconds", value)}
					/>
					<NumberField
						label="Fingerprint point differences"
						hint="Maximum Hamming-bit distance for matching points."
						value={settings.maximumFingerprintPointDifferences}
						onChange={(value) => update("maximumFingerprintPointDifferences", value)}
					/>
					<NumberField
						label="Maximum time gap (seconds)"
						hint="Largest gap allowed inside one shared sequence."
						value={settings.maximumTimeSkipSeconds}
						step={0.1}
						onChange={(value) => update("maximumTimeSkipSeconds", value)}
					/>
					<NumberField
						label="Inverted-index shift"
						hint="Point-value shift considered while finding alignment."
						value={settings.invertedIndexShift}
						onChange={(value) => update("invertedIndexShift", value)}
					/>
				</div>
				<div className="mt-7 flex flex-wrap gap-2">
					<button
						onClick={() => void save()}
						disabled={saving}
						className="console-button rounded-lg px-4 py-2.5 text-sm disabled:opacity-50"
					>
						{saving ? "Saving…" : "Save settings"}
					</button>
					<button
						onClick={() => void runNow()}
						disabled={!task}
						className="flex items-center gap-2 rounded-lg border console-divider px-4 py-2.5 text-sm console-muted hover:bg-white/10 disabled:opacity-50"
					>
						<IconPlayerPlay size={16} />
						Run now
					</button>
					<button
						onClick={() => setConfirmClear(true)}
						className="ml-auto flex items-center gap-2 rounded-lg border border-red-400/30 px-4 py-2.5 text-sm text-red-200 hover:bg-red-400/10"
					>
						<IconTrash size={16} />
						Remove all detected ranges
					</button>
				</div>
			</SurfaceCard>
			<SurfaceCard className="mt-5 p-6">
				<p className="console-kicker">Detection task</p>
				{task ? (
					<div className="mt-3 space-y-2 text-sm">
						<p>
							<span className="console-muted">Status: </span>
							{task.triggers?.length ? task.lastState || "idle" : "paused"}
						</p>
						<p>
							<span className="console-muted">Schedule: </span>
							{task.triggers?.length ? task.triggers.map((trigger) => trigger.type === "interval" ? `every ${Math.round((trigger.intervalSeconds || 0) / 60)} minutes` : trigger.type).join(", ") : "disabled (no triggers)"}
						</p>
						{task.lastMessage && <p className="console-muted">{task.lastMessage}</p>}
						{task.nextRunAt && (
							<p className="console-muted">
								Next run {new Date(task.nextRunAt).toLocaleString()}
							</p>
						)}
					</div>
				) : (
					<p className="mt-3 text-sm console-muted">
						The task will appear after the Orchestrator starts with the latest schema.
					</p>
				)}
			</SurfaceCard>
		</div>
	);
}
