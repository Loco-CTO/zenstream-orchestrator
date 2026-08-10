"use client";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { saveSession } from "../dashboard/components/admin-client";

export default function AdminLogin() {
	const router = useRouter();
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [error, setError] = useState("");
	const [busy, setBusy] = useState(false);

	async function submit(event: FormEvent) {
		event.preventDefault();
		setBusy(true);
		setError("");
		const response = await fetch("/api/admin/login", {
			method: "POST",
			headers: { Username: username, Password: password },
		});
		if (response.ok) {
			const profile = (await response.json()) as { username: string };
			saveSession({ username: profile.username });
			router.push("/web/dashboard");
		} else setError("Invalid administrator credentials.");
		setBusy(false);
	}

	return (
		<main className="console-root flex min-h-screen items-center justify-center p-6">
			<form
				onSubmit={submit}
				className="console-card w-full max-w-md rounded-3xl p-8"
			>
				<div className="mb-8">
					<img
						src="/assets/icons/icon.png"
						alt="ZenStream"
						className="mb-5 h-14 w-14 rounded-2xl"
					/>
					<p className="console-wordmark text-xs font-black">ZENSTREAM</p>
					<h1 className="mt-3 text-3xl font-black">Orchestrator console</h1>
					<p className="mt-2 text-sm console-muted">Administrator access only.</p>
				</div>
				<div className="space-y-4">
					<input
						aria-label="Username"
						required
						value={username}
						onChange={(e) => setUsername(e.target.value)}
						placeholder="Username"
						className="console-input h-12 w-full rounded-xl px-4 outline-none placeholder:text-white/30"
					/>
					<input
						aria-label="Password"
						required
						type="password"
						value={password}
						onChange={(e) => setPassword(e.target.value)}
						placeholder="Password"
						className="console-input h-12 w-full rounded-xl px-4 outline-none placeholder:text-white/30"
					/>
				</div>
				{error && (
					<p role="alert" className="mt-4 text-sm text-red-200">
						{error}
					</p>
				)}
				<button
					disabled={busy}
					className="console-button mt-6 h-12 w-full rounded-xl font-semibold transition disabled:opacity-50"
				>
					{busy ? "Signing in…" : "Sign in"}
				</button>
			</form>
		</main>
	);
}
