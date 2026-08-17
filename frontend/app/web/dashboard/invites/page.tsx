"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { IconCopy, IconKey, IconPlus, IconTrash } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	ConfirmDialog,
	EmptyState,
	PageHeader,
	StatusMessage,
	SurfaceCard,
	DashboardModal,
} from "../components/dashboard-surface";

type Library = { id: string; name: string; type: string };
type Invite = {
	id: string;
	tokenFingerprint: string;
	maxUses: number | null;
	usedUses: number;
	expiresAt: string | null;
	createdAt: string;
	status: "active" | "exhausted" | "expired";
	libraryIds: string[];
	libraries: Array<{ id: string; name: string }>;
};

type DurationUnit = "seconds" | "minutes" | "hours" | "days";

const unitSeconds: Record<DurationUnit, number> = {
	seconds: 1,
	minutes: 60,
	hours: 60 * 60,
	days: 24 * 60 * 60,
};

function dateLabel(value: string | null) {
	if (!value) return "Never";
	const date = new Date(value);
	return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function statusClass(status: Invite["status"]) {
	if (status === "active") return "text-[#5ee3d8]";
	if (status === "expired") return "text-[#f07070]";
	return "text-[#f0bf6a]";
}

export default function InvitesPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [invites, setInvites] = useState<Invite[]>([]);
	const [libraries, setLibraries] = useState<Library[]>([]);
	const [loading, setLoading] = useState(true);
	const [message, setMessage] = useState("");
	const [createOpen, setCreateOpen] = useState(false);
	const [busy, setBusy] = useState(false);
	const [inviteLink, setInviteLink] = useState("");
	const [publicWebUrl, setPublicWebUrl] = useState("");
	const [inviteToDelete, setInviteToDelete] = useState<Invite | null>(null);
	const [selectedLibraries, setSelectedLibraries] = useState<string[]>([]);
	const [unlimitedUses, setUnlimitedUses] = useState(false);
	const [maxUses, setMaxUses] = useState("1");
	const [neverExpires, setNeverExpires] = useState(false);
	const [expiryValue, setExpiryValue] = useState("7");
	const [expiryUnit, setExpiryUnit] = useState<DurationUnit>("days");

	async function load(current = session) {
		if (!current) return;
		setLoading(true);
		const [inviteResponse, libraryResponse, publicWebResponse] =
			await Promise.all([
				adminFetch("/api/admin/invites", current),
				adminFetch("/api/admin/libraries", current),
				adminFetch("/api/config/public-web-url", current),
			]);
		if (inviteResponse.ok) {
			const payload = (await inviteResponse.json()) as { invites?: Invite[] };
			setInvites(payload.invites || []);
		}
		if (libraryResponse.ok)
			setLibraries((await libraryResponse.json()) as Library[]);
		if (publicWebResponse.ok) {
			const payload = (await publicWebResponse.json()) as {
				publicWebUrl?: string;
			};
			setPublicWebUrl((payload.publicWebUrl || "").replace(/\/+$/, ""));
		} else setPublicWebUrl("");
		if (!inviteResponse.ok || !libraryResponse.ok)
			setMessage("Could not load invite information.");
		setLoading(false);
	}

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) void load(current);
		// The initial session is intentionally captured once on mount.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, []);

	function resetDraft() {
		setSelectedLibraries([]);
		setUnlimitedUses(false);
		setMaxUses("1");
		setNeverExpires(false);
		setExpiryValue("7");
		setExpiryUnit("days");
		setInviteLink("");
	}

	function openCreate() {
		resetDraft();
		setMessage("");
		setCreateOpen(true);
	}

	function closeCreate() {
		if (busy) return;
		setCreateOpen(false);
		setInviteLink("");
	}

	function toggleLibrary(libraryId: string) {
		setSelectedLibraries((current) =>
			current.includes(libraryId)
				? current.filter((value) => value !== libraryId)
				: [...current, libraryId],
		);
	}

	async function createInvite(event: FormEvent<HTMLFormElement>) {
		event.preventDefault();
		if (!session || inviteLink) return;
		if (!publicWebUrl) {
			setMessage("The public web URL is not configured for this dashboard build.");
			return;
		}
		const uses = Number(maxUses);
		const duration = Number(expiryValue);
		if (!unlimitedUses && (!Number.isInteger(uses) || uses < 1)) {
			setMessage("Enter a positive maximum use count or choose Unlimited.");
			return;
		}
		if (!neverExpires && (!Number.isInteger(duration) || duration < 1)) {
			setMessage("Enter a positive expiry duration or choose Never expires.");
			return;
		}
		setBusy(true);
		setMessage("");
		const response = await adminFetch("/api/admin/invites", session, {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({
				libraryIds: selectedLibraries,
				maxUses: unlimitedUses ? null : uses,
				expiresInSeconds: neverExpires ? null : duration * unitSeconds[expiryUnit],
			}),
		});
		if (!response.ok) {
			setMessage(
				(await response.json().catch(() => null))?.detail ||
					"Could not create invite.",
			);
			setBusy(false);
			return;
		}
		const payload = (await response.json()) as { token?: string };
		if (!payload.token) {
			setMessage("The server did not return an invite token.");
			setBusy(false);
			return;
		}
		setInviteLink(
			`${publicWebUrl}/register?invite=${encodeURIComponent(payload.token)}`,
		);
		setMessage(
			"Invite created. Copy this link now; it cannot be recovered later.",
		);
		await load(session);
		setBusy(false);
	}

	async function copyLink() {
		if (!inviteLink) return;
		await navigator.clipboard.writeText(inviteLink);
		setMessage("Invite copied to clipboard.");
	}

	async function deleteInvite(invite: Invite) {
		if (!session) return;
		setBusy(true);
		const response = await adminFetch(
			`/api/admin/invites/${encodeURIComponent(invite.id)}`,
			session,
			{ method: "DELETE" },
		);
		setMessage(
			response.ok ? "Invite revoked and deleted." : "Could not revoke invite.",
		);
		if (response.ok)
			setInvites((current) => current.filter((value) => value.id !== invite.id));
		setInviteToDelete(null);
		setBusy(false);
	}

	const visibleLibraries = useMemo(
		() => new Map(libraries.map((library) => [library.id, library.name])),
		[libraries],
	);

	return (
		<div className="max-w-6xl">
			<ConfirmDialog
				open={Boolean(inviteToDelete)}
				title="Revoke invite?"
				description="This permanently deletes the invite and its remaining access rules. Anyone with the link will no longer be able to use it."
				confirmLabel="Revoke invite"
				destructive
				busy={busy}
				onClose={() => setInviteToDelete(null)}
				onConfirm={() => inviteToDelete && void deleteInvite(inviteToDelete)}
			/>
			<DashboardModal
				open={createOpen}
				onClose={closeCreate}
				title={inviteLink ? "Invite link ready" : "Create invite"}
				closeDisabled={busy}
			>
				<form onSubmit={createInvite}>
					{inviteLink ? (
						<>
							<p className="mt-5 text-sm leading-6 console-muted">
								Treat this link as a credential. It is shown only once.
							</p>
							<div className="mt-5 break-all rounded-xl border border-[#5ee3d8]/25 bg-[#5ee3d8]/10 p-4 text-sm text-[#d8fffb]">
								{inviteLink}
							</div>
							<div className="mt-6 flex justify-end gap-3">
								<button
									type="button"
									onClick={copyLink}
									className="console-button flex items-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold"
								>
									<IconCopy size={16} /> Copy link
								</button>
								<button
									type="button"
									onClick={closeCreate}
									className="material-icon-button h-11 px-4 text-sm font-semibold"
								>
									Done
								</button>
							</div>
						</>
					) : (
						<>
							<p className="mt-5 text-sm leading-6 console-muted">
								Choose the libraries and limits for the new account.
							</p>
							<label className="mt-6 block text-sm font-semibold text-white">
								Libraries
							</label>
							<div className="mt-3 grid max-h-44 gap-2 overflow-y-auto rounded-xl border console-divider p-3 sm:grid-cols-2">
								{libraries.length ? (
									libraries.map((library) => (
										<label
											key={library.id}
											className="flex items-center gap-3 rounded-lg px-2 py-2 text-sm console-muted hover:bg-white/5"
										>
											<input
												type="checkbox"
												checked={selectedLibraries.includes(library.id)}
												onChange={() => toggleLibrary(library.id)}
											/>
											<span>{library.name}</span>
										</label>
									))
								) : (
									<span className="col-span-full p-2 text-sm console-muted">
										No libraries configured. The account will start without catalog
										access.
									</span>
								)}
							</div>
							<div className="mt-6 grid gap-5 sm:grid-cols-2">
								<div>
									<label className="block text-sm font-semibold text-white">
										Usage limit
									</label>
									<div className="mt-2 flex gap-2">
										<input
											type="number"
											min={1}
											value={maxUses}
											disabled={unlimitedUses}
											onChange={(event) => setMaxUses(event.target.value)}
											className="console-input h-10 w-full rounded-lg px-3 text-sm outline-none"
										/>
										<label className="flex items-center gap-2 whitespace-nowrap text-sm console-muted">
											<input
												type="checkbox"
												checked={unlimitedUses}
												onChange={(event) => setUnlimitedUses(event.target.checked)}
											/>{" "}
											Unlimited
										</label>
									</div>
								</div>
								<div>
									<label className="block text-sm font-semibold text-white">
										Expiry
									</label>
									<div className="mt-2 flex gap-2">
										<input
											type="number"
											min={1}
											value={expiryValue}
											disabled={neverExpires}
											onChange={(event) => setExpiryValue(event.target.value)}
											className="console-input h-10 min-w-0 flex-1 rounded-lg px-3 text-sm outline-none"
										/>
										<select
											value={expiryUnit}
											disabled={neverExpires}
											onChange={(event) =>
												setExpiryUnit(event.target.value as DurationUnit)
											}
											className="console-input h-10 w-28 rounded-lg px-3 text-sm outline-none"
										>
											<option value="seconds">seconds</option>
											<option value="minutes">minutes</option>
											<option value="hours">hours</option>
											<option value="days">days</option>
										</select>
									</div>
									<label className="mt-2 flex items-center gap-2 text-sm console-muted">
										<input
											type="checkbox"
											checked={neverExpires}
											onChange={(event) => setNeverExpires(event.target.checked)}
										/>{" "}
										Never expires
									</label>
								</div>
							</div>
							{message && <StatusMessage>{message}</StatusMessage>}
							<div className="mt-7 flex justify-end gap-3">
								<button
									type="button"
									disabled={busy}
									onClick={closeCreate}
									className="material-icon-button h-11 px-4 text-sm font-semibold"
								>
									Cancel
								</button>
								<button
									type="submit"
									disabled={busy}
									className="console-button h-11 rounded-xl px-4 text-sm font-semibold disabled:opacity-60"
								>
									{busy ? "Creating…" : "Create invite"}
								</button>
							</div>
						</>
					)}
				</form>
			</DashboardModal>

			<PageHeader
				title="Invites"
				description="Create public-web registration links with controlled library access, usage, and expiry."
				actions={
					<button
						onClick={openCreate}
						className="console-button flex items-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold"
					>
						<IconPlus size={16} /> Create invite
					</button>
				}
			/>
			{message && !createOpen && <StatusMessage>{message}</StatusMessage>}
			<SurfaceCard className="mt-7 p-0">
				{loading ? (
					<EmptyState>Loading invites…</EmptyState>
				) : invites.length === 0 ? (
					<EmptyState>No invites have been created.</EmptyState>
				) : (
					<div className="divide-y console-divider">
						{invites.map((invite) => (
							<div
								key={invite.id}
								className="flex flex-col gap-5 p-5 lg:flex-row lg:items-center lg:justify-between"
							>
								<div className="min-w-0">
									<div className="flex flex-wrap items-center gap-3">
										<IconKey size={17} className="text-[#5ee3d8]" />
										<span className="font-mono text-sm text-white">
											{invite.tokenFingerprint}…
										</span>
										<span
											className={`text-xs font-semibold uppercase tracking-[0.12em] ${statusClass(invite.status)}`}
										>
											{invite.status}
										</span>
									</div>
									<p className="mt-2 text-sm console-muted">
										{invite.usedUses} / {invite.maxUses === null ? "∞" : invite.maxUses}{" "}
										uses · expires {dateLabel(invite.expiresAt)}
									</p>
									<p className="mt-1 text-xs console-muted">
										Libraries:{" "}
										{invite.libraries.length
											? invite.libraries
													.map((library) => visibleLibraries.get(library.id) || library.name)
													.join(", ")
											: "None"}
									</p>
								</div>
								<div className="flex items-center justify-between gap-4 lg:justify-end">
									<span className="text-xs console-muted">
										Created {dateLabel(invite.createdAt)}
									</span>
									<button
										onClick={() => setInviteToDelete(invite)}
										className="dashboard-danger-button flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-semibold"
									>
										<IconTrash size={15} /> Revoke
									</button>
								</div>
							</div>
						))}
					</div>
				)}
			</SurfaceCard>
		</div>
	);
}
