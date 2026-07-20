"use client";
import { useState } from "react";
import { adminFetch, readSession } from "../components/admin-client";
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
		<div>
			<h1 className="pb-5 text-3xl font-semibold tracking-tight">Invites</h1>
			<div className="console-card mt-8 max-w-2xl rounded-2xl p-6">
				<h2 className="text-xl font-bold">Create a registration invite</h2>
				<p className="mt-2 text-sm leading-6 console-muted">
					Generate a link for a new ZenStream user. Treat invite links as
					credentials.
				</p>
				{invite && (
					<div className="mt-6 break-all rounded-xl border border-[#55c9b0]/25 bg-[#55c9b0]/10 p-4 text-sm text-[#b7f5e6]">
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
				{message && <p className="mt-4 text-sm text-[#8fe4cf]">{message}</p>}
			</div>
		</div>
	);
}
