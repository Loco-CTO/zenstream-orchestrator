"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import {
	IconBan,
	IconKey,
	IconPlus,
	IconRefresh,
	IconTrash,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	ConfirmDialog,
	EmptyState,
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";

type User = {
	id: string;
	username: string;
	disabled: boolean;
	libraryIds: string[];
};
type Library = { id: string; name: string; type: string };

export default function UsersPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [users, setUsers] = useState<User[]>([]);
	const [libraries, setLibraries] = useState<Library[]>([]);
	const [query, setQuery] = useState("");
	const [message, setMessage] = useState("");
	const [accountDialog, setAccountDialog] = useState<"create" | "reset" | null>(
		null,
	);
	const [targetUser, setTargetUser] = useState<User | null>(null);
	const [draftUsername, setDraftUsername] = useState("");
	const [draftPassword, setDraftPassword] = useState("");
	const [accountBusy, setAccountBusy] = useState(false);
	const [userToDelete, setUserToDelete] = useState<User | null>(null);

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
		() =>
			users.filter((user) =>
				user.username.toLowerCase().includes(query.toLowerCase()),
			),
		[users, query],
	);

	function openCreateUser() {
		setDraftUsername("");
		setDraftPassword("");
		setTargetUser(null);
		setAccountDialog("create");
	}

	function openPasswordReset(user: User) {
		setDraftPassword("");
		setTargetUser(user);
		setAccountDialog("reset");
	}

	async function submitAccount(event: FormEvent<HTMLFormElement>) {
		event.preventDefault();
		if (!session || !accountDialog) return;
		setAccountBusy(true);
		const response =
			accountDialog === "create"
				? await adminFetch("/api/admin/users", session, {
						method: "POST",
						headers: { "Content-Type": "application/json" },
						body: JSON.stringify({
							username: draftUsername.trim(),
							password: draftPassword,
						}),
					})
				: await adminFetch(
						`/api/admin/users/${encodeURIComponent(targetUser?.id || "")}/reset-password`,
						session,
						{
							method: "POST",
							headers: { "Content-Type": "application/json" },
							body: JSON.stringify({ password: draftPassword }),
						},
					);
		setMessage(
			response.ok
				? accountDialog === "create"
					? "User created with no library access."
					: "Password reset and existing sessions revoked."
				: (await response.json().catch(() => null))?.detail ||
						(accountDialog === "create"
							? "Could not create user."
							: "Could not reset password."),
		);
		if (response.ok) {
			setAccountDialog(null);
			void load();
		}
		setAccountBusy(false);
	}

	async function setAccess(user: User, libraryId: string, allowed: boolean) {
		if (!session) return;
		const libraryIds = allowed
			? [...new Set([...user.libraryIds, libraryId])]
			: user.libraryIds.filter((value) => value !== libraryId);
		setUsers((current) =>
			current.map((value) =>
				value.id === user.id ? { ...value, libraryIds } : value,
			),
		);
		const response = await adminFetch(
			`/api/admin/users/${encodeURIComponent(user.id)}/libraries`,
			session,
			{
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ libraryIds }),
			},
		);
		if (!response.ok) {
			setMessage("Could not update library access.");
			void load();
		}
	}

	async function toggleDisabled(user: User) {
		if (!session) return;
		const response = await adminFetch(
			`/api/admin/users/${encodeURIComponent(user.id)}`,
			session,
			{
				method: "PATCH",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ disabled: !user.disabled }),
			},
		);
		setMessage(
			response.ok
				? user.disabled
					? "User enabled."
					: "User disabled and sessions revoked."
				: "Could not update user.",
		);
		if (response.ok) void load();
	}

	async function deleteUser(user: User) {
		if (!session) return;
		const response = await adminFetch(
			`/api/admin/users/${encodeURIComponent(user.id)}`,
			session,
			{ method: "DELETE" },
		);
		setMessage(response.ok ? "User deleted." : "Could not delete user.");
		if (response.ok) void load();
		setUserToDelete(null);
	}

	return (
		<div className="max-w-6xl">
			<ConfirmDialog
				open={Boolean(userToDelete)}
				title="Delete user?"
				description={`Delete ${userToDelete?.username || "this user"}. This permanently removes their preferences and watch state.`}
				confirmLabel="Delete user"
				destructive
				onClose={() => setUserToDelete(null)}
				onConfirm={() => userToDelete && void deleteUser(userToDelete)}
			/>
			{accountDialog && (
				<div className="dashboard-dialog-layer" role="presentation">
					<button
						type="button"
						className="dashboard-dialog-backdrop"
						aria-label="Close dialog"
						disabled={accountBusy}
						onClick={() => setAccountDialog(null)}
					/>
					<form
						onSubmit={submitAccount}
						className="dashboard-dialog"
						role="dialog"
						aria-modal="true"
						aria-labelledby="account-dialog-title"
					>
						<p className="console-kicker">Account access</p>
						<h2 id="account-dialog-title" className="mt-2 text-xl font-semibold">
							{accountDialog === "create"
								? "Create user"
								: `Reset ${targetUser?.username || "user"} password`}
						</h2>
						<p className="mt-3 text-sm leading-6 console-muted">
							{accountDialog === "create"
								? "New accounts start with no library access."
								: "Existing sessions will be revoked after the password is reset."}
						</p>
						{accountDialog === "create" && (
							<label className="mt-6 block text-sm">
								<span className="console-muted">Username</span>
								<input
									autoFocus
									required
									value={draftUsername}
									onChange={(event) => setDraftUsername(event.target.value)}
									className="console-input mt-2 h-11 w-full rounded-xl px-3 outline-none"
								/>
							</label>
						)}
						<label className="mt-5 block text-sm">
							<span className="console-muted">
								{accountDialog === "create" ? "Temporary password" : "New password"}
							</span>
							<input
								required
								minLength={8}
								type="password"
								value={draftPassword}
								onChange={(event) => setDraftPassword(event.target.value)}
								className="console-input mt-2 h-11 w-full rounded-xl px-3 outline-none"
							/>
						</label>
						<div className="mt-7 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
							<button
								type="button"
								disabled={accountBusy}
								onClick={() => setAccountDialog(null)}
								className="material-icon-button h-11 px-4 text-sm font-semibold"
							>
								Cancel
							</button>
							<button
								disabled={accountBusy}
								className="console-button h-11 rounded-xl px-4 text-sm font-semibold disabled:opacity-60"
							>
								{accountBusy
									? "Saving…"
									: accountDialog === "create"
										? "Create user"
										: "Reset password"}
							</button>
						</div>
					</form>
				</div>
			)}
			<PageHeader
				title="Users"
				description="Create accounts, manage access to libraries, and control user sessions."
				actions={
					<>
						<button
							onClick={() => void load()}
							className="material-icon-button"
							aria-label="Refresh users"
						>
							<IconRefresh size={17} />
						</button>
						<div className="flex min-w-0 flex-1 gap-3 sm:flex-none">
							<input
								value={query}
								onChange={(event) => setQuery(event.target.value)}
								placeholder="Search users"
								className="console-input h-11 rounded-xl px-4 text-sm outline-none placeholder:text-white/30"
							/>
							<button
								onClick={openCreateUser}
								className="console-button flex items-center gap-2 rounded-xl px-4 text-sm font-semibold"
							>
								<IconPlus size={16} />
								Create user
							</button>
						</div>
					</>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			<div className="mt-7 space-y-4">
				{filtered.map((user) => (
					<SurfaceCard key={user.id} className="p-5">
						<div className="flex items-center justify-between gap-4">
							<div>
								<p className="font-semibold">{user.username}</p>
								<p className="mt-1 text-xs console-muted">
									{user.disabled ? "Disabled" : "Active"} · deny by default
								</p>
							</div>
							<div className="flex items-center gap-2">
								<span className="mr-2 text-xs console-muted">
									{user.libraryIds.length} libraries
								</span>
								<button
									onClick={() => openPasswordReset(user)}
									className="material-icon-button"
									aria-label={`Reset ${user.username} password`}
									title="Reset password"
								>
									<IconKey size={16} />
								</button>
								<button
									onClick={() => void toggleDisabled(user)}
									className="material-icon-button"
									aria-label={`${user.disabled ? "Enable" : "Disable"} ${user.username}`}
									title={user.disabled ? "Enable user" : "Disable user"}
								>
									<IconBan size={16} />
								</button>
								<button
									onClick={() => setUserToDelete(user)}
									className="material-icon-button text-[#5ee3d8]"
									aria-label={`Delete ${user.username}`}
									title="Delete user"
								>
									<IconTrash size={16} />
								</button>
							</div>
						</div>
						<div className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
							{libraries.map((library) => {
								const checked = user.libraryIds.includes(library.id);
								return (
									<label
										key={library.id}
										className="flex cursor-pointer items-center gap-3 rounded-xl border console-divider px-3 py-3 text-sm"
									>
										<input
											type="checkbox"
											checked={checked}
											onChange={(event) =>
												void setAccess(user, library.id, event.target.checked)
											}
											className="accent-[#5ee3d8]"
										/>
										<span className="min-w-0">
											<span className="block truncate font-medium">{library.name}</span>
											<span className="text-[11px] console-muted">{library.type}</span>
										</span>
									</label>
								);
							})}
						</div>
					</SurfaceCard>
				))}
				{filtered.length === 0 && (
					<SurfaceCard>
						<EmptyState>No users found.</EmptyState>
					</SurfaceCard>
				)}
			</div>
		</div>
	);
}
