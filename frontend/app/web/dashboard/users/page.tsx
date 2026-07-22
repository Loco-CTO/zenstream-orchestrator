"use client";

import { useEffect, useMemo, useState } from "react";
import { IconBan, IconKey, IconPlus, IconRefresh, IconTrash } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";

type User = { id: string; username: string; disabled: boolean; libraryIds: string[] };
type Library = { id: string; name: string; type: string };

export default function UsersPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [users, setUsers] = useState<User[]>([]);
	const [libraries, setLibraries] = useState<Library[]>([]);
	const [query, setQuery] = useState("");
	const [message, setMessage] = useState("");

	async function load(current = session) {
		if (!current) return;
		const [userResponse, libraryResponse] = await Promise.all([
			adminFetch("/api/admin/users", current),
			adminFetch("/api/admin/libraries", current),
		]);
		if (userResponse.ok) setUsers((await userResponse.json()).users || []);
		if (libraryResponse.ok) setLibraries(await libraryResponse.json());
	}

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) void load(current);
	}, []);

	const filtered = useMemo(
		() => users.filter((user) => user.username.toLowerCase().includes(query.toLowerCase())),
		[users, query],
	);

	async function createUser() {
		if (!session) return;
		const username = window.prompt("Username:")?.trim();
		if (!username) return;
		const password = window.prompt("Temporary password (8+ characters):") || "";
		const response = await adminFetch("/api/admin/users", session, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ username, password }),
		});
		setMessage(response.ok ? "User created with no library access." : ((await response.json().catch(() => null))?.detail || "Could not create user."));
		if (response.ok) void load();
	}

	async function setAccess(user: User, libraryId: string, allowed: boolean) {
		if (!session) return;
		const libraryIds = allowed
			? [...new Set([...user.libraryIds, libraryId])]
			: user.libraryIds.filter((value) => value !== libraryId);
		setUsers((current) => current.map((value) => value.id === user.id ? { ...value, libraryIds } : value));
		const response = await adminFetch(`/api/admin/users/${encodeURIComponent(user.id)}/libraries`, session, {
			method: "PUT",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ libraryIds }),
		});
		if (!response.ok) {
			setMessage("Could not update library access.");
			void load();
		}
	}

	async function resetPassword(user: User) {
		if (!session) return;
		const password = window.prompt(`New password for ${user.username} (8+ characters):`) || "";
		if (!password) return;
		const response = await adminFetch(`/api/admin/users/${encodeURIComponent(user.id)}/reset-password`, session, {
			method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
		});
		setMessage(response.ok ? "Password reset and existing sessions revoked." : "Could not reset password.");
	}

	async function toggleDisabled(user: User) {
		if (!session) return;
		const response = await adminFetch(`/api/admin/users/${encodeURIComponent(user.id)}`, session, {
			method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ disabled: !user.disabled }),
		});
		setMessage(response.ok ? (user.disabled ? "User enabled." : "User disabled and sessions revoked.") : "Could not update user.");
		if (response.ok) void load();
	}

	async function deleteUser(user: User) {
		if (!session || !window.confirm(`Delete ${user.username}? This permanently removes their preferences and watch state.`)) return;
		const response = await adminFetch(`/api/admin/users/${encodeURIComponent(user.id)}`, session, { method: "DELETE" });
		setMessage(response.ok ? "User deleted." : "Could not delete user.");
		if (response.ok) void load();
	}

	return (
		<div>
			<div className="flex flex-col justify-between gap-4 pb-5 sm:flex-row sm:items-center">
				<div className="flex items-center gap-3">
					<h1 className="text-3xl font-semibold tracking-tight">Users</h1>
					<button onClick={() => void load()} className="material-icon-button" aria-label="Refresh users"><IconRefresh size={17} /></button>
				</div>
				<div className="flex gap-3">
					<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search users" className="console-input h-11 rounded-xl px-4 text-sm outline-none placeholder:text-white/30" />
					<button onClick={() => void createUser()} className="console-button flex items-center gap-2 rounded-xl px-4 text-sm font-semibold"><IconPlus size={16} />Create user</button>
				</div>
			</div>
			{message && <p className="mt-4 text-sm text-[#8fe4cf]">{message}</p>}
			<div className="mt-8 space-y-4">
				{filtered.map((user) => (
					<section key={user.id} className="console-card rounded-2xl p-5">
						<div className="flex items-center justify-between gap-4">
							<div><p className="font-semibold">{user.username}</p><p className="mt-1 text-xs console-muted">{user.disabled ? "Disabled" : "Active"} · deny by default</p></div>
							<div className="flex items-center gap-2">
								<span className="mr-2 text-xs console-muted">{user.libraryIds.length} libraries</span>
								<button onClick={() => void resetPassword(user)} className="material-icon-button" aria-label={`Reset ${user.username} password`} title="Reset password"><IconKey size={16} /></button>
								<button onClick={() => void toggleDisabled(user)} className="material-icon-button" aria-label={`${user.disabled ? "Enable" : "Disable"} ${user.username}`} title={user.disabled ? "Enable user" : "Disable user"}><IconBan size={16} /></button>
								<button onClick={() => void deleteUser(user)} className="material-icon-button text-red-300" aria-label={`Delete ${user.username}`} title="Delete user"><IconTrash size={16} /></button>
							</div>
						</div>
						<div className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
							{libraries.map((library) => {
								const checked = user.libraryIds.includes(library.id);
								return <label key={library.id} className="flex cursor-pointer items-center gap-3 rounded-xl border console-divider px-3 py-3 text-sm">
									<input type="checkbox" checked={checked} onChange={(event) => void setAccess(user, library.id, event.target.checked)} className="accent-[#8fe4cf]" />
									<span className="min-w-0"><span className="block truncate font-medium">{library.name}</span><span className="text-[11px] console-muted">{library.type}</span></span>
								</label>;
							})}
						</div>
					</section>
				))}
				{filtered.length === 0 && <p className="console-card rounded-2xl p-10 text-center text-sm console-muted">No users found.</p>}
			</div>
		</div>
	);
}
