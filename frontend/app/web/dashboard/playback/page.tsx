"use client";

import { FormEvent, useEffect, useState } from "react";
import { IconPlayerPlay, IconRefresh } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";

type PlaybackSettings = {
	maxTranscodes: number;
	maxTranscodesPerUser: number;
};

const DEFAULTS: PlaybackSettings = {
	maxTranscodes: 2,
	maxTranscodesPerUser: 1,
};

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
			setSettings({
				maxTranscodes: Number(data?.maxTranscodes) || DEFAULTS.maxTranscodes,
				maxTranscodesPerUser:
					Number(data?.maxTranscodesPerUser) ||
					DEFAULTS.maxTranscodesPerUser,
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
		setSaving(true);
		const response = await adminFetch("/api/admin/playback/settings", session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(settings),
		});
		const data = await response.json().catch(() => null);
		setMessage(
			response.ok
				? "Playback limits saved. New sessions use the updated limits."
				: data?.detail || "Could not save playback limits.",
		);
		if (response.ok) setSettings(data);
		setSaving(false);
	}

	return (
		<div>
			<div className="flex items-center gap-3 pb-5">
				<div>
					<p className="console-kicker">Server capacity</p>
					<h1 className="mt-2 text-3xl font-semibold tracking-tight">
						Playback
					</h1>
				</div>
				<button
					onClick={() => session && void load(session)}
					className="material-icon-button"
					aria-label="Refresh playback settings"
					title="Refresh playback settings"
				>
					<IconRefresh size={17} />
				</button>
			</div>
			<section className="console-card max-w-3xl rounded-2xl p-6">
				<div className="flex items-start justify-between gap-4">
					<div>
						<h2 className="text-xl font-bold">Transcoding limits</h2>
						<p className="mt-3 text-sm leading-6 console-muted">
							Limit concurrent FFmpeg sessions to keep the server responsive.
							Direct play and remux sessions do not count against these limits.
						</p>
					</div>
					<IconPlayerPlay className="text-[#8fe4cf]" size={22} />
				</div>
				<form onSubmit={save} className="mt-6 space-y-5">
					<label className="block">
						<span className="text-sm font-semibold">Maximum global transcodes</span>
						<span className="mt-1 block text-xs console-muted">
							Maximum number of active FFmpeg playback sessions across all users.
						</span>
						<input
							type="number"
							min={1}
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
						<span className="text-sm font-semibold">Maximum transcodes per user</span>
						<span className="mt-1 block text-xs console-muted">
							Prevents one account from using all global transcoding capacity.
						</span>
						<input
							type="number"
							min={1}
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
							disabled={loading || saving}
							className="console-button rounded-xl px-4 py-3 text-sm font-semibold disabled:opacity-40"
						>
							{saving ? "Saving…" : "Save playback limits"}
						</button>
						{message && <p className="text-sm console-muted">{message}</p>}
					</div>
				</form>
			</section>
		</div>
	);
}
