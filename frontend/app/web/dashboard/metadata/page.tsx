"use client";

import { FormEvent, useEffect, useState } from "react";
import { IconCheck, IconKey, IconMusic, IconRefresh, IconX } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";

type ProviderState = { configured: boolean; credentialType?: string; validatedAt?: string | null };
type ProviderMap = Record<string, ProviderState>;

export default function MetadataPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [providers, setProviders] = useState<ProviderMap>({});
	const [tmdb, setTmdb] = useState("");
	const [tmdbType, setTmdbType] = useState("api_key");
	const [tvdb, setTvdb] = useState("");
	const [pin, setPin] = useState("");
	const [message, setMessage] = useState("");

	async function load(current: Session) {
		const response = await adminFetch("/api/admin/metadata/providers", current);
		if (response.ok) setProviders(await response.json());
	}
	useEffect(() => {
		const current = readSession();
		if (current) {
			setSession(current);
			load(current);
		}
	}, []);

	async function save(event: FormEvent, provider: "tmdb" | "tvdb") {
		event.preventDefault();
		if (!session) return;
		const body = provider === "tmdb"
			? { credential: tmdb, credentialType: tmdbType, validate: true }
			: { apiKey: tvdb, pin, validate: true };
		const response = await adminFetch(`/api/admin/metadata/providers/${provider}`, session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify(body),
		});
		setMessage(response.ok ? `${provider.toUpperCase()} credentials saved.` : (await response.json().catch(() => null))?.detail || "Provider validation failed.");
		if (response.ok) {
			if (provider === "tmdb") setTmdb("");
			else { setTvdb(""); setPin(""); }
			load(session);
		}
	}

	async function clear(provider: "tmdb" | "tvdb") {
		if (!session) return;
		await adminFetch(`/api/admin/metadata/providers/${provider}`, session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ clear: true }),
		});
		setMessage(`${provider.toUpperCase()} credentials cleared.`);
		load(session);
	}

	return (
		<div>
			<p className="console-kicker">Metadata</p>
			<h1 className="mt-3 text-4xl font-black tracking-tight">Provider connections</h1>
			<p className="mt-2 max-w-2xl text-sm leading-6 console-muted">Credentials are encrypted by the orchestrator and are never returned to the browser. Metadata is fetched per language when a library preview needs it.</p>
			<div className="mt-8 grid gap-6 lg:grid-cols-2">
				<form onSubmit={(event) => save(event, "tmdb")} className="console-card rounded-2xl p-6">
					<div className="flex items-start justify-between"><div><p className="console-kicker">Movies and secondary TV metadata</p><h2 className="mt-2 text-xl font-bold">TMDB</h2></div><IconKey className="text-[#8fe4cf]" size={22} /></div>
					<p className="mt-3 text-sm console-muted">Use a TMDB v3 API key or v4 read access token.</p>
					<select value={tmdbType} onChange={(event) => setTmdbType(event.target.value)} className="console-input mt-5 h-11 w-full rounded-xl px-4 text-sm outline-none"><option value="api_key">v3 API key</option><option value="read_access_token">v4 read access token</option></select>
					<input value={tmdb} onChange={(event) => setTmdb(event.target.value)} required={!providers.tmdb?.configured} placeholder="Credential" type="password" className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30" />
					<ProviderStatus state={providers.tmdb} />
					<div className="mt-5 flex gap-3"><button className="console-button rounded-xl px-4 py-3 text-sm font-semibold">Save and validate</button>{providers.tmdb?.configured && <button type="button" onClick={() => clear("tmdb")} className="rounded-xl border console-divider px-4 py-3 text-sm console-muted">Clear</button>}</div>
				</form>
				<form onSubmit={(event) => save(event, "tvdb")} className="console-card rounded-2xl p-6">
					<div className="flex items-start justify-between"><div><p className="console-kicker">Series and collection metadata</p><h2 className="mt-2 text-xl font-bold">TheTVDB</h2></div><IconKey className="text-[#8fe4cf]" size={22} /></div>
					<p className="mt-3 text-sm console-muted">A subscriber PIN is optional for licensed keys and required for some user-supported keys.</p>
					<input value={tvdb} onChange={(event) => setTvdb(event.target.value)} required={!providers.tvdb?.configured} placeholder="v4 API key" type="password" className="console-input mt-5 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30" />
					<input value={pin} onChange={(event) => setPin(event.target.value)} placeholder="Subscriber PIN (optional)" type="password" className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30" />
					<ProviderStatus state={providers.tvdb} />
					<div className="mt-5 flex gap-3"><button className="console-button rounded-xl px-4 py-3 text-sm font-semibold">Save and validate</button>{providers.tvdb?.configured && <button type="button" onClick={() => clear("tvdb")} className="rounded-xl border console-divider px-4 py-3 text-sm console-muted">Clear</button>}</div>
				</form>
				<div className="console-card rounded-2xl p-6 lg:col-span-2"><div className="flex items-start gap-4"><span className="rounded-xl bg-[#55c9b0]/10 p-3 text-[#8fe4cf]"><IconMusic size={22} /></span><div><p className="console-kicker">Music</p><h2 className="mt-2 text-xl font-bold">MusicBrainz and Cover Art Archive</h2><p className="mt-2 text-sm leading-6 console-muted">MusicBrainz does not require an API key. ZenStream identifies requests with its application user agent and uses local artwork before querying the Cover Art Archive.</p><p className="mt-4 text-xs console-muted">Metadata provided by <a className="text-[#8fe4cf]" href="https://musicbrainz.org/" target="_blank" rel="noreferrer">MusicBrainz</a> and artwork by the <a className="text-[#8fe4cf]" href="https://coverartarchive.org/" target="_blank" rel="noreferrer">Cover Art Archive</a>.</p></div></div></div>
			</div>
			{message && <p className="mt-5 flex items-center gap-2 text-sm text-[#8fe4cf]"><IconRefresh size={16} />{message}</p>}
			<p className="mt-8 text-xs leading-5 console-muted">This product uses the TMDB API but is not endorsed or certified by TMDB. Metadata provided by <a className="text-[#8fe4cf]" href="https://thetvdb.com/" target="_blank" rel="noreferrer">TheTVDB</a>.</p>
		</div>
	);
}

function ProviderStatus({ state }: { state?: ProviderState }) {
	return <p className="mt-4 flex items-center gap-2 text-xs console-muted">{state?.configured ? <><IconCheck size={15} className="text-[#8fe4cf]" />Configured{state.validatedAt ? ` · validated ${new Date(state.validatedAt).toLocaleString()}` : ""}</> : <><IconX size={15} />Not configured</>}</p>;
}
