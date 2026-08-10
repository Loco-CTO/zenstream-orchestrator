"use client";
import { FormEvent, useEffect, useState } from "react";
import {
	adminFetch,
	readSession,
	saveSession,
	Session,
} from "../components/admin-client";
import { PageHeader, StatusMessage } from "../components/dashboard-surface";
export default function ProfilePage() {
	const [session, setSession] = useState<Session | null>(null);
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [message, setMessage] = useState("");
	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) setUsername(current.username);
	}, []);
	async function submit(event: FormEvent) {
		event.preventDefault();
		if (!session) return;
		const r = await adminFetch("/api/admin/profile", session, {
			method: "PATCH",
			headers: {
				...(username !== session.username ? { "New-Username": username } : {}),
				...(password ? { "New-Password": password } : {}),
			},
		});
		if (!r.ok) {
			setMessage((await r.json()).message || "Could not update profile.");
			return;
		}
		const data = await r.json();
		const next = { username: data.username };
		saveSession(next);
		setSession(next);
		setPassword("");
		setMessage("Profile updated successfully.");
	}
	return (
		<div className="max-w-3xl">
			<PageHeader
				title="Profile & security"
				description="Update the credentials used to administer this ZenStream server."
			/>
			<form
				onSubmit={submit}
				className="console-card mt-7 max-w-xl rounded-2xl p-6"
			>
				<h2 className="text-xl font-bold">Administrator credentials</h2>
				<p className="mt-2 text-sm leading-6 console-muted">
					Your current session will stay active after saving.
				</p>
				<label className="mt-6 block text-sm console-muted">
					Username
					<input
						required
						value={username}
						onChange={(e) => setUsername(e.target.value)}
						className="console-input mt-2 h-11 w-full rounded-xl px-4 outline-none"
					/>
				</label>
				<label className="mt-4 block text-sm console-muted">
					New password
					<input
						minLength={8}
						type="password"
						value={password}
						onChange={(e) => setPassword(e.target.value)}
						placeholder="Leave blank to keep current password"
						className="console-input mt-2 h-11 w-full rounded-xl px-4 outline-none placeholder:text-white/30"
					/>
				</label>
				<button className="console-button mt-6 rounded-xl px-4 py-3 text-sm font-semibold">
					Save changes
				</button>
				{message && <StatusMessage>{message}</StatusMessage>}
			</form>
		</div>
	);
}
