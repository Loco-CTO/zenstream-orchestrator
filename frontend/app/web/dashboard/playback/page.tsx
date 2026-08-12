"use client";

import { FormEvent, useEffect, useState } from "react";
import { IconPhoto, IconPlayerPlay, IconRefresh } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type PlaybackSettings = {
	maxTranscodes: number;
	maxTranscodesPerUser: number;
	trickplayFrameWidth: number;
	trickplayIntervalSeconds: number;
	trickplayWorkers: number;
};

const DEFAULTS: PlaybackSettings = {
	maxTranscodes: 0,
	maxTranscodesPerUser: 0,
	trickplayFrameWidth: 320,
	trickplayIntervalSeconds: 10,
	trickplayWorkers: 1,
};

function validTrickplaySettings(
	width: number,
	intervalSeconds: number,
	workers: number,
) {
	return (
		Number.isInteger(width) &&
		width >= 160 &&
		width <= 640 &&
		width % 16 === 0 &&
		Number.isInteger(intervalSeconds) &&
		intervalSeconds >= 1 &&
		intervalSeconds <= 60 &&
		Number.isInteger(workers) &&
		workers >= 1 &&
		workers <= 64
	);
}

export default function PlaybackPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [settings, setSettings] = useState<PlaybackSettings>(DEFAULTS);
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);
	const [message, setMessage] = useState("");

	async function load(current: Session) {
		setLoading(true);
		const response = await adminFetch("/api/admin/playback/settings", current);
		const data = await response.json().catch(() => null);
		if (response.ok) {
			const maxTranscodes = Number(data?.maxTranscodes);
			const maxTranscodesPerUser = Number(data?.maxTranscodesPerUser);
			const trickplayFrameWidth = Number(data?.trickplayFrameWidth);
			const trickplayIntervalSeconds = Number(data?.trickplayIntervalSeconds);
			const trickplayWorkers = Number(data?.trickplayWorkers);
			setSettings({
				maxTranscodes: Number.isFinite(maxTranscodes)
					? maxTranscodes
					: DEFAULTS.maxTranscodes,
				maxTranscodesPerUser: Number.isFinite(maxTranscodesPerUser)
					? maxTranscodesPerUser
					: DEFAULTS.maxTranscodesPerUser,
				trickplayFrameWidth:
					Number.isFinite(trickplayFrameWidth) && trickplayFrameWidth > 0
						? trickplayFrameWidth
						: DEFAULTS.trickplayFrameWidth,
				trickplayIntervalSeconds:
					Number.isFinite(trickplayIntervalSeconds) && trickplayIntervalSeconds > 0
						? trickplayIntervalSeconds
						: DEFAULTS.trickplayIntervalSeconds,
				trickplayWorkers:
					Number.isFinite(trickplayWorkers) && trickplayWorkers >= 1
						? trickplayWorkers
						: DEFAULTS.trickplayWorkers,
			});
		} else {
			setMessage(data?.detail || "Could not load playback settings.");
		}
		setLoading(false);
	}

	useEffect(() => {
		const current = readSession();
		if (current) {
			setSession(current);
			void load(current);
		}
	}, []);

	async function save(event: FormEvent) {
		event.preventDefault();
		if (!session) return;
		if (
			!validTrickplaySettings(
				settings.trickplayFrameWidth,
				settings.trickplayIntervalSeconds,
				settings.trickplayWorkers,
			)
		) {
			setMessage(
				"Choose a frame width from 160 to 640 in 16 px steps, an interval from 1 to 60 seconds, and 1 to 64 workers.",
			);
			return;
		}
		setSaving(true);
		const response = await adminFetch("/api/admin/playback/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				...settings,
				trickplayFrameHeight: (settings.trickplayFrameWidth * 9) / 16,
			}),
		});
		const data = await response.json().catch(() => null);
		setMessage(
			response.ok
				? "Playback settings saved. New sessions use the updated limits."
				: data?.detail || "Could not save playback limits.",
		);
		if (response.ok) {
			setSettings((current) => ({
				maxTranscodes: Number.isFinite(Number(data?.maxTranscodes))
					? Number(data.maxTranscodes)
					: current.maxTranscodes,
				maxTranscodesPerUser: Number.isFinite(Number(data?.maxTranscodesPerUser))
					? Number(data.maxTranscodesPerUser)
					: current.maxTranscodesPerUser,
				trickplayFrameWidth:
					Number.isFinite(Number(data?.trickplayFrameWidth)) &&
					Number(data.trickplayFrameWidth) > 0
						? Number(data.trickplayFrameWidth)
						: current.trickplayFrameWidth,
				trickplayIntervalSeconds:
					Number.isFinite(Number(data?.trickplayIntervalSeconds)) &&
					Number(data.trickplayIntervalSeconds) > 0
						? Number(data.trickplayIntervalSeconds)
						: current.trickplayIntervalSeconds,
				trickplayWorkers:
					Number.isFinite(Number(data?.trickplayWorkers)) &&
					Number(data.trickplayWorkers) >= 1
						? Number(data.trickplayWorkers)
						: current.trickplayWorkers,
			}));
		}
		setSaving(false);
	}

	const trickplayFrameWidth = Math.max(1, settings.trickplayFrameWidth);
	const trickplayFrameHeight = (trickplayFrameWidth * 9) / 16;
	const trickplaySettingsAreValid = validTrickplaySettings(
		settings.trickplayFrameWidth,
		settings.trickplayIntervalSeconds,
		settings.trickplayWorkers,
	);

	return (
		<div className="max-w-3xl">
			<PageHeader
				title="Playback"
				description="Set the server capacity available to HLS transcodes and timeline preview extraction."
				actions={
					<button
						onClick={() => session && void load(session)}
						className="material-icon-button"
						aria-label="Refresh playback settings"
						title="Refresh playback settings"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			<SurfaceCard className="mt-7 p-6">
				<div className="flex items-start justify-between gap-4">
					<div>
						<h2 className="text-xl font-bold">Transcoding limits</h2>
						<p className="mt-3 text-sm leading-6 console-muted">
							Limit concurrent FFmpeg sessions to keep the server responsive. Enter 0
							for unlimited. Direct play and remux sessions do not count against these
							limits.
						</p>
					</div>
					<IconPlayerPlay className="text-[#5ee3d8]" size={22} />
				</div>
				<form onSubmit={save} className="mt-6 space-y-5">
					<label className="block">
						<span className="text-sm font-semibold">Maximum global transcodes</span>
						<span className="mt-1 block text-xs console-muted">
							Maximum number of active FFmpeg playback sessions across all users; 0 is
							unlimited.
						</span>
						<input
							type="number"
							min={0}
							max={64}
							step={1}
							value={settings.maxTranscodes}
							onChange={(event) =>
								setSettings((current) => ({
									...current,
									maxTranscodes: Number(event.target.value),
								}))
							}
							className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none"
						/>
					</label>
					<label className="block">
						<span className="text-sm font-semibold">Trickplay workers</span>
						<span className="mt-1 block text-xs console-muted">
							Maximum concurrent FFmpeg trickplay extractions. Use 1–64 workers.
						</span>
						<input
							type="number"
							min={1}
							max={64}
							step={1}
							value={settings.trickplayWorkers}
							onChange={(event) =>
								setSettings((current) => ({
									...current,
									trickplayWorkers: Number(event.target.value),
								}))
							}
							className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none"
						/>
					</label>
					<label className="block">
						<span className="text-sm font-semibold">Maximum transcodes per user</span>
						<span className="mt-1 block text-xs console-muted">
							Prevents one account from using all global transcoding capacity; 0 is
							unlimited.
						</span>
						<input
							type="number"
							min={0}
							max={64}
							step={1}
							value={settings.maxTranscodesPerUser}
							onChange={(event) =>
								setSettings((current) => ({
									...current,
									maxTranscodesPerUser: Number(event.target.value),
								}))
							}
							className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none"
						/>
					</label>
					<div className="flex items-center gap-3">
						<button
							type="submit"
							disabled={loading || saving || !trickplaySettingsAreValid}
							className="console-button rounded-xl px-4 py-3 text-sm font-semibold disabled:opacity-40"
						>
							{saving ? "Saving…" : "Save playback limits"}
						</button>
					</div>
				</form>
			</SurfaceCard>
			<SurfaceCard className="mt-5 p-6">
				<div className="flex items-start justify-between gap-4">
					<div>
						<h2 className="text-xl font-bold">Timeline previews</h2>
						<p className="mt-3 text-sm leading-6 console-muted">
							Choose the size of each extracted timeline-preview frame. Frames are
							always 16:9; narrower source media is fitted fully inside a black frame.
						</p>
					</div>
					<IconPhoto className="text-[#5ee3d8]" size={22} />
				</div>
				<form onSubmit={save} className="mt-6 space-y-5">
					<label className="block">
						<span className="text-sm font-semibold">Frame width</span>
						<span className="mt-1 block text-xs console-muted">
							Frame height is locked to 16:9: {trickplayFrameWidth} ×{" "}
							{trickplayFrameHeight} px.
						</span>
						<input
							type="number"
							min={160}
							max={640}
							step={16}
							value={settings.trickplayFrameWidth}
							onChange={(event) =>
								setSettings((current) => ({
									...current,
									trickplayFrameWidth: Number(event.target.value),
								}))
							}
							className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none"
						/>
					</label>
					<div>
						<span className="text-sm font-semibold">Presets</span>
						<div className="mt-3 flex flex-wrap gap-2">
							{[160, 320, 640].map((width) => (
								<button
									key={width}
									type="button"
									onClick={() =>
										setSettings((current) => ({
											...current,
											trickplayFrameWidth: width,
										}))
									}
									className={`rounded-lg border px-3 py-2 text-sm font-semibold transition ${settings.trickplayFrameWidth === width ? "border-[#5ee3d8] bg-[#5ee3d8]/15 text-[#d8fffb]" : "border-white/15 text-white/75 hover:border-white/35"}`}
								>
									{width} × {(width * 9) / 16}
								</button>
							))}
						</div>
					</div>
					<label className="block">
						<span className="text-sm font-semibold">Frame interval</span>
						<span className="mt-1 block text-xs console-muted">
							Seconds between extracted frames. Shorter intervals make seeking previews
							more precise and use more storage.
						</span>
						<input
							type="number"
							min={1}
							max={60}
							step={1}
							value={settings.trickplayIntervalSeconds}
							onChange={(event) =>
								setSettings((current) => ({
									...current,
									trickplayIntervalSeconds: Number(event.target.value),
								}))
							}
							className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none"
						/>
					</label>
					<div className="flex items-center gap-3">
						<button
							type="submit"
							disabled={loading || saving || !trickplaySettingsAreValid}
							className="console-button rounded-xl px-4 py-3 text-sm font-semibold disabled:opacity-40"
						>
							{saving ? "Saving…" : "Save timeline preview settings"}
						</button>
					</div>
				</form>
			</SurfaceCard>
		</div>
	);
}
