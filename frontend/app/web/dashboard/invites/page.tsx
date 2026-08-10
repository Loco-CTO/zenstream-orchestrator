"use client";
import { useState } from "react";
import { adminFetch, readSession } from "../components/admin-client";
import {
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";
export default function InvitesPage() {
	const [invite, setInvite] = useState("");
	const [message, setMessage] = useState("");
	async function generate() {
		const session = readSession();
		if (!session) return;
		const r = await adminFetch("/api/user/generate_invite", session, {
			method: "POST",
		});
		if (!r.ok) {
			setMessage("Could not generate invite.");
			return;
		}
		const data = await r.json();
		setInvite(`${location.origin}/web/register?invite=${data.inviteid}`);
		setMessage("Invite generated.");
	}
	async function copy() {
		await navigator.clipboard.writeText(invite);
		setMessage("Invite copied to clipboard.");
	}
	return (
		<div className="max-w-3xl">
			<PageHeader
				title="Invites"
				description="Create a secure, single-use registration link for a new ZenStream user."
			/>
			<SurfaceCard className="mt-7 max-w-2xl p-6">
				<h2 className="text-xl font-bold">Create a registration invite</h2>
				<p className="mt-2 text-sm leading-6 console-muted">
					Generate a link for a new ZenStream user. Treat invite links as
					credentials.
				</p>
				{invite && (
					<div className="mt-6 break-all rounded-xl border border-[#aeb9ff]/25 bg-[#aeb9ff]/10 p-4 text-sm text-[#e8eaff]">
						{invite}
					</div>
				)}
				<div className="mt-5 flex gap-3">
					<button
						onClick={generate}
						className="console-button rounded-xl px-4 py-3 text-sm font-semibold"
					>
						Generate invite
					</button>
					{invite && (
						<button
							onClick={copy}
							className="rounded-xl border console-divider px-4 py-3 text-sm console-muted hover:bg-white/10"
						>
							Copy link
						</button>
					)}
				</div>
				{message && <StatusMessage>{message}</StatusMessage>}
			</SurfaceCard>
		</div>
	);
}
