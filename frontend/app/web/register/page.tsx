"use client";
import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

export default function RegisterPage() {
	const router = useRouter();
	const [invite, setInvite] = useState("");
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [message, setMessage] = useState("");
	useEffect(
		() =>
			setInvite(new URLSearchParams(window.location.search).get("invite") || ""),
		[],
	);
	async function submit(event: FormEvent) {
		event.preventDefault();
		const response = await fetch("/api/user/register", {
			method: "POST",
			headers: { Username: username, Password: password, url: invite },
		});
		if (response.status === 201) {
			setMessage("Account created. Redirecting to administrator login…");
			setTimeout(() => router.push("/web/login"), 900);
		} else
			setMessage("This invite is invalid or the username is already in use.");
	}
	return (
		<main className="console-root flex min-h-screen items-center justify-center p-6">
			<form
				onSubmit={submit}
				className="console-card w-full max-w-md rounded-2xl p-8"
			>
				<img
					src="/icons/icon.png"
					alt="ZenStream"
					className="mb-5 h-14 w-14 rounded-2xl"
				/>
				<p className="console-wordmark text-xs font-black">ZENSTREAM</p>
				<h1 className="mt-3 text-3xl font-black">Create an account</h1>
				<div className="mt-8 space-y-4">
					<input
						required
						value={username}
						onChange={(e) => setUsername(e.target.value)}
						placeholder="Username"
						className="console-input h-12 w-full rounded-md px-4 outline-none placeholder:text-white/30"
					/>
					<input
						required
						type="password"
						value={password}
						onChange={(e) => setPassword(e.target.value)}
						placeholder="Password"
						className="console-input h-12 w-full rounded-md px-4 outline-none placeholder:text-white/30"
					/>
				</div>
				{message && <p className="mt-4 text-sm console-muted">{message}</p>}
				<button className="console-button mt-6 h-12 w-full rounded-md font-semibold">
					Register
				</button>
			</form>
		</main>
	);
}
