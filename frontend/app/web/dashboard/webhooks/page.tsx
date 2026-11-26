"use client";

import { IconPlus, IconWebhook } from "@tabler/icons-react";
import { useEffect, useState, type FormEvent } from "react";
import { adminFetch, readSession } from "../components/admin-client";
import { PageHeader, SurfaceCard } from "../components/dashboard-surface";

type Webhook = {
	id: string;
	name: string;
	events: string[];
	lastResult: string | null;
	testPending: boolean;
};
type Settings = { events: Record<string, string>; webhooks: Webhook[] };
type Draft = { id?: string; name: string; url: string; events: string[] };

const input = "console-input mt-2 h-11 w-full rounded-xl px-4 text-sm";
const primary =
	"console-button-primary rounded-xl px-4 py-2 text-sm font-semibold";
const secondary = "console-input rounded-xl px-3 py-2 text-sm";

async function request(path = "", init?: RequestInit) {
	const session = readSession();
	if (!session) throw new Error("Sign in as an administrator.");
	const response = await adminFetch(`/api/admin/webhooks${path}`, session, init);
	if (!response.ok) {
		const body = await response.json().catch(() => null);
		throw new Error(
			typeof body?.detail === "string"
				? body.detail
				: "The webhook request failed. Try again.",
		);
	}
	return response.status === 204 ? null : response.json();
}

export default function WebhooksPage() {
	const [settings, setSettings] = useState<Settings | null>(null);
	const [draft, setDraft] = useState<Draft | null>(null);
	const [removing, setRemoving] = useState<string | null>(null);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState("");
	const [notice, setNotice] = useState("");

	async function load() {
		setSettings(await request());
	}

	useEffect(() => {
		void load().catch((failure: Error) => setError(failure.message));
	}, []);

	useEffect(() => {
		if (busy || !settings?.webhooks.some((hook) => hook.testPending)) return;
		let active = true;
		const timer = window.setInterval(() => {
			void request()
				.then((value) => {
					if (active) setSettings(value);
				})
				.catch(() => undefined);
		}, 2000);
		return () => {
			active = false;
			window.clearInterval(timer);
		};
	}, [busy, settings]);

	async function action(run: () => Promise<void>) {
		setBusy(true);
		setError("");
		setNotice("");
		try {
			await run();
		} catch (failure) {
			setError(
				failure instanceof Error ? failure.message : "The webhook request failed.",
			);
		} finally {
			setBusy(false);
		}
	}

	function openDraft(value: Draft) {
		setDraft(value);
		setRemoving(null);
		setError("");
		setNotice("");
	}

	function save(event: FormEvent) {
		event.preventDefault();
		if (!draft) return;
		void action(async () => {
			const { id, name, url, events } = draft;
			setSettings(
				await request(id ? `/${id}` : "", {
					method: id ? "PUT" : "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ name, events, ...(url.trim() ? { url } : {}) }),
				}),
			);
			setDraft(null);
			setNotice(id ? "Webhook updated." : "Webhook added.");
		});
	}

	const events = Object.entries(settings?.events ?? {});
	const groups = [
		{ label: "Accounts", prefixes: ["user."] },
		{ label: "Playback", prefixes: ["playback.", "music."] },
		{ label: "Library", prefixes: ["library."] },
	];

	return (
		<div className="max-w-4xl space-y-5">
			<PageHeader
				title="Webhooks"
				description="Send ZenStream activity to Discord channels."
				actions={
					<button
						type="button"
						className={primary}
						disabled={!settings || busy || !!draft}
						onClick={() => openDraft({ name: "", url: "", events: [] })}
					>
						<span className="flex items-center gap-2">
							<IconPlus size={16} aria-hidden /> Add webhook
						</span>
					</button>
				}
			/>
			{error && (
				<p role="alert" className="text-sm text-red-300">
					{error}
				</p>
			)}
			{notice && (
				<p role="status" className="text-sm text-[#5ee3d8]">
					{notice}
				</p>
			)}
			{draft && (
				<SurfaceCard>
					<h2 className="text-lg font-semibold">
						{draft.id ? "Edit webhook" : "Add a Discord webhook"}
					</h2>
					<form onSubmit={save} className="mt-4 space-y-5" autoComplete="off">
						<fieldset disabled={busy} className="min-w-0 space-y-4">
							<label className="block text-sm font-semibold">
								Name
								<input
									className={input}
									value={draft.name}
									maxLength={80}
									required
									onChange={(event) => setDraft({ ...draft, name: event.target.value })}
									placeholder="e.g. Library activity"
								/>
							</label>
							<label className="block text-sm font-semibold">
								Discord webhook URL
								<input
									className={input}
									type="password"
									autoComplete="new-password"
									value={draft.url}
									maxLength={512}
									required={!draft.id}
									onChange={(event) => setDraft({ ...draft, url: event.target.value })}
									placeholder={
										draft.id
											? "Leave blank to keep the saved URL"
											: "https://discord.com/api/webhooks/…"
									}
								/>
								<span className="mt-2 block text-xs font-normal console-muted">
									Copy the incoming webhook URL from your Discord channel’s Integrations
									settings.
								</span>
							</label>
							<div className="flex flex-wrap items-center justify-between gap-2">
								<h3 className="text-sm font-semibold">
									Subscribed events{" "}
									<span className="ml-1 font-normal console-muted">
										({draft.events.length} selected)
									</span>
								</h3>
								<div className="flex gap-2">
									<button
										type="button"
										className={secondary}
										onClick={() =>
											setDraft({ ...draft, events: events.map(([key]) => key) })
										}
									>
										Select all
									</button>
									<button
										type="button"
										className={secondary}
										onClick={() => setDraft({ ...draft, events: [] })}
									>
										Clear all
									</button>
								</div>
							</div>
							<div className="grid gap-4 sm:grid-cols-2">
								{groups.map(({ label, prefixes }) => (
									<fieldset key={label} className="min-w-0">
										<legend className="mb-1 text-xs font-semibold console-muted">
											{label}
										</legend>
										{events
											.filter(([key]) => prefixes.some((prefix) => key.startsWith(prefix)))
											.map(([key, text]) => (
												<label
													key={key}
													className="flex min-h-8 items-center gap-2 text-sm"
												>
													<input
														type="checkbox"
														className="accent-[#5ee3d8]"
														checked={draft.events.includes(key)}
														onChange={(event) =>
															setDraft({
																...draft,
																events: event.target.checked
																	? [...draft.events, key]
																	: draft.events.filter((entry) => entry !== key),
															})
														}
													/>
													{text}
												</label>
											))}
									</fieldset>
								))}
							</div>
							<p className="text-xs console-muted">
								A webhook with no events selected stays idle. Video progress is sent at
								25%, 50%, and 75%, at most once per milestone in a viewer session.
							</p>
							<div className="flex flex-wrap gap-2">
								<button type="submit" className={primary}>
									{busy ? "Saving…" : "Save webhook"}
								</button>
								<button
									type="button"
									className={secondary}
									onClick={() => setDraft(null)}
								>
									Cancel
								</button>
							</div>
						</fieldset>
					</form>
				</SurfaceCard>
			)}
			{!settings && !error && (
				<p role="status" className="text-sm console-muted">
					Loading webhooks…
				</p>
			)}
			{settings?.webhooks.length === 0 && !draft && (
				<SurfaceCard>
					<IconWebhook size={22} aria-hidden className="mb-3 text-[#5ee3d8]" />
					<h2 className="text-base font-semibold">No webhooks yet</h2>
					<p className="mt-1 text-sm console-muted">
						Add a Discord webhook and choose the activity you want to receive.
					</p>
				</SurfaceCard>
			)}
			{settings?.webhooks.map((hook) => (
				<SurfaceCard key={hook.id}>
					<div className="flex flex-wrap items-start justify-between gap-4">
						<div className="min-w-0">
							<h2 className="break-words text-base font-semibold">{hook.name}</h2>
							<p className="mt-1 text-sm console-muted">
								{hook.events.length === 0
									? "No events subscribed"
									: hook.events.length === events.length
										? "All events subscribed"
										: `${hook.events.length} events subscribed`}
							</p>
							{hook.lastResult && (
								<p className="mt-1 text-xs console-muted" role="status">
									{hook.testPending ? "Test message pending" : hook.lastResult}
								</p>
							)}
						</div>
						<div className="flex flex-wrap gap-2">
							<button
								type="button"
								className={secondary}
								disabled={busy || !!draft || !!removing}
								onClick={() =>
									openDraft({
										id: hook.id,
										name: hook.name,
										events: hook.events,
										url: "",
									})
								}
							>
								Edit
							</button>
							<button
								type="button"
								className={secondary}
								disabled={busy || !!draft || !!removing}
								onClick={() =>
									void action(async () => {
										await request(`/${hook.id}/test`, { method: "POST" });
										await load();
									})
								}
							>
								Send test
							</button>
							<button
								type="button"
								className={secondary}
								disabled={busy || !!draft || !!removing}
								onClick={() => setRemoving(hook.id)}
							>
								Remove
							</button>
						</div>
					</div>
					{removing === hook.id && (
						<div className="mt-4 flex flex-wrap items-center gap-2">
							<p className="text-sm">Remove this webhook and its queued messages?</p>
							<button
								type="button"
								className={`${secondary} text-red-300`}
								disabled={busy}
								onClick={() =>
									void action(async () => {
										await request(`/${hook.id}`, { method: "DELETE" });
										setRemoving(null);
										await load();
										setNotice("Webhook removed.");
									})
								}
							>
								Remove webhook
							</button>
							<button
								type="button"
								className={secondary}
								disabled={busy}
								onClick={() => setRemoving(null)}
							>
								Cancel
							</button>
						</div>
					)}
				</SurfaceCard>
			))}
		</div>
	);
}
