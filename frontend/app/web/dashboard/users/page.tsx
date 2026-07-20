"use client";
import { useEffect, useMemo, useState } from "react";
import { IconRefresh } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
type User = { username: string; disabled: boolean };
export default function UsersPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [users, setUsers] = useState<User[]>([]);
	const [query, setQuery] = useState("");
	const [message, setMessage] = useState("");
	async function load(current = session) {
		if (!current) return;
		const r = await adminFetch("/api/admin/users", current);
		if (r.ok) setUsers(await r.json());
	}
	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) load(current);
	}, []);
	const filtered = useMemo(
		() =>
			users.filter((user) =>
				user.username.toLowerCase().includes(query.toLowerCase()),
			),
		[users, query],
	);
	async function action(
		username: string,
		operation: "disable" | "enable" | "reset" | "delete",
	) {
		if (!session) return;
		if (
			(operation === "delete" || operation === "reset") &&
			!window.confirm(
				`${operation === "delete" ? "Delete" : "Reset the password for"} ${username}?`,
			)
		)
			return;
		const options: RequestInit =
			operation === "reset"
				? {
						method: "PATCH",
						headers: {
							"New-Password":
								window.prompt("New password (8+ characters):") || "",
						},
					}
				: operation === "delete"
					? { method: "DELETE" }
					: { method: "PATCH" };
		const path =
			operation === "disable" || operation === "enable"
				? `/api/admin/users/${encodeURIComponent(username)}?disabled=${operation === "disable"}`
				: `/api/admin/users/${encodeURIComponent(username)}`;
		const r = await adminFetch(path, session, options);
		setMessage(r.ok ? "User updated." : "The user action failed.");
		load();
	}
	return (
		<div>
			<div className="flex flex-col justify-between gap-4 pb-5 sm:flex-row sm:items-center">
				<div>
					<div className="flex items-center gap-3"><h1 className="text-3xl font-semibold tracking-tight">Users</h1><button onClick={() => load()} className="material-icon-button" aria-label="Refresh users" title="Refresh users"><IconRefresh size={17} /></button></div>
				</div>
				<input
					value={query}
					onChange={(e) => setQuery(e.target.value)}
					placeholder="Search users"
					className="console-input h-11 rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
				/>
			</div>
			{message && <p className="mt-4 text-sm text-[#8fe4cf]">{message}</p>}
			<div className="console-card mt-8 overflow-hidden rounded-2xl">
				<div className="grid grid-cols-[1fr_auto_auto] gap-4 border-b console-divider px-5 py-4 text-[10px] font-bold uppercase tracking-[.16em] console-muted">
					<span>Username</span>
					<span>Status</span>
					<span>Actions</span>
				</div>
				{filtered.map((user) => (
					<div
						key={user.username}
						className="grid grid-cols-[1fr_auto_auto] items-center gap-4 border-b console-divider px-5 py-4 text-sm last:border-0"
					>
						<span className="font-semibold">{user.username}</span>
						<span
							className={`rounded-full px-2.5 py-1 text-xs ${user.disabled ? "bg-red-200/15 text-red-200" : "bg-[#55c9b0]/12 text-[#8fe4cf]"}`}
						>
							{user.disabled ? "Disabled" : "Active"}
						</span>
						<div className="flex gap-2">
							<button
								onClick={() =>
									action(user.username, user.disabled ? "enable" : "disable")
								}
								className="rounded-lg border console-divider px-3 py-2 text-xs console-muted hover:bg-white/10"
							>
								{user.disabled ? "Enable" : "Disable"}
							</button>
							<button
								onClick={() => action(user.username, "reset")}
								className="rounded-lg border console-divider px-3 py-2 text-xs console-muted hover:bg-white/10"
							>
								Reset
							</button>
							<button
								onClick={() => action(user.username, "delete")}
								className="rounded-lg border border-red-200/20 px-3 py-2 text-xs text-red-200 hover:bg-red-200/10"
							>
								Delete
							</button>
						</div>
					</div>
				))}
				{filtered.length === 0 && (
					<p className="p-10 text-center text-sm console-muted">
						No users found.
					</p>
				)}
			</div>
		</div>
	);
}
