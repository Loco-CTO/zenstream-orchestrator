"use client";
import { FormEvent, useEffect, useState } from "react";
import { IconRefresh } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";
type Admin = { username: string; is_root: boolean; disabled: boolean };
export default function AdministratorsPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [accounts, setAccounts] = useState<Admin[]>([]);
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [message, setMessage] = useState("");
	async function load(current = session) {
		if (!current) return;
		const r = await adminFetch("/api/admin/accounts", current);
		if (r.ok) setAccounts(await r.json());
	}
	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) load(current);
	}, []);
	async function create(event: FormEvent) {
		event.preventDefault();
		if (!session) return;
		const r = await adminFetch("/api/admin/accounts", session, {
			method: "POST",
			headers: { "Target-Username": username, "New-Password": password },
		});
		setMessage(
			r.ok ? "Administrator created." : "Could not create administrator.",
		);
		if (r.ok) {
			setUsername("");
			setPassword("");
			load();
		}
	}
	async function toggle(account: Admin) {
		if (!session || account.is_root) return;
		const r = await adminFetch(
			`/api/admin/accounts/${encodeURIComponent(account.username)}?disabled=${!account.disabled}`,
			session,
			{ method: "PATCH" },
		);
		setMessage(
			r.ok ? "Administrator updated." : "Could not update administrator.",
		);
		load();
	}
	return (
		<div className="max-w-6xl">
			<PageHeader
				title="Administrators"
				description="Manage the local accounts that can configure this ZenStream server."
				actions={
					<button
						onClick={() => load()}
						className="material-icon-button"
						aria-label="Refresh administrators"
						title="Refresh administrators"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			<div className="mt-7 grid gap-6 lg:grid-cols-[1.4fr_1fr]">
				<SurfaceCard className="overflow-hidden">
					<div className="border-b console-divider px-5 py-4 text-[10px] font-bold uppercase tracking-[.16em] console-muted">
						Local administrators
					</div>
					{accounts.map((account) => (
						<div
							key={account.username}
							className="flex items-center justify-between border-b console-divider px-5 py-4 last:border-0"
						>
							<div>
								<p className="font-semibold">{account.username}</p>
								<p className="mt-1 text-xs console-muted">
									{account.is_root
										? "Root administrator"
										: account.disabled
											? "Disabled"
											: "Administrator"}
								</p>
							</div>
							{!account.is_root && (
								<button
									onClick={() => toggle(account)}
									className="rounded-lg border console-divider px-3 py-2 text-xs console-muted hover:bg-white/10"
								>
									{account.disabled ? "Enable" : "Disable"}
								</button>
							)}
						</div>
					))}
				</SurfaceCard>
				<form onSubmit={create} className="console-card rounded-2xl p-6">
					<h2 className="text-xl font-bold">Add administrator</h2>
					<p className="mt-2 text-sm console-muted">
						Create a local console account.
					</p>
					<input
						required
						value={username}
						onChange={(e) => setUsername(e.target.value)}
						placeholder="Username"
						className="console-input mt-5 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
					/>
					<input
						required
						minLength={8}
						type="password"
						value={password}
						onChange={(e) => setPassword(e.target.value)}
						placeholder="Password (8+ characters)"
						className="console-input mt-3 h-11 w-full rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
					/>
					<button className="console-button mt-4 rounded-xl px-4 py-3 text-sm font-semibold">
						Create administrator
					</button>
					{message && <StatusMessage>{message}</StatusMessage>}
				</form>
			</div>
		</div>
	);
}
