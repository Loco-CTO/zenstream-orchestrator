"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { apiUrl } from "../api-url";

export default function RegisterPage() {
	const router = useRouter();
	const [invite, setInvite] = useState("");
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [message, setMessage] = useState("");
	const [loading, setLoading] = useState(false);
	const usernameRef = useRef<HTMLInputElement>(null);
	useEffect(() => {
		setInvite(new URLSearchParams(window.location.search).get("invite") || "");
		usernameRef.current?.focus();
	}, []);
	async function submit(event: FormEvent) {
		event.preventDefault();
		setLoading(true);
		const response = await fetch(apiUrl("/api/user/register"), {
			method: "POST",
			headers: { Username: username, Password: password, url: invite },
		});
		if (response.status === 201) {
			setMessage("Account created. Redirecting to administrator login…");
			window.setTimeout(() => router.push("/web/login"), 900);
		} else
			setMessage("This invite is invalid or the username is already in use.");
		setLoading(false);
	}
	const inputStyle: React.CSSProperties = {
		width: "100%",
		background: "#0a0a0a",
		border: "1px solid #1c1c1c",
		borderRadius: 9,
		padding: "13px 16px",
		color: "#ddd",
		fontSize: 14,
		fontFamily: "var(--font-sans)",
	};
	return (
		<main
			className="login-design"
			style={{
				minHeight: "100vh",
				background: "#000",
				display: "flex",
				alignItems: "center",
				justifyContent: "center",
				position: "relative",
				overflow: "hidden",
			}}
		>
			<div
				style={{
					position: "absolute",
					width: 600,
					height: 600,
					borderRadius: "50%",
					background:
						"radial-gradient(circle, rgba(94,227,216,0.04) 0%, transparent 70%)",
					top: "50%",
					left: "50%",
					transform: "translate(-50%,-50%)",
					pointerEvents: "none",
				}}
			/>
			<div style={{ width: "100%", maxWidth: 380, padding: "0 24px" }}>
				<form
					onSubmit={submit}
					style={{
						background: "#080808",
						borderRadius: 16,
						padding: "40px 36px",
						boxShadow: "0 0 0 1px #141414, 0 32px 80px rgba(0,0,0,0.6)",
					}}
				>
					<div
						style={{
							display: "flex",
							alignItems: "center",
							gap: 10,
							marginBottom: 28,
						}}
					>
						<img
							src="/icons/icon.png"
							alt="ZenStream"
							style={{ width: 34, height: 34 }}
						/>
						<span
							style={{
								fontSize: 11,
								fontWeight: 700,
								letterSpacing: ".18em",
								textTransform: "uppercase",
								color: "var(--primary)",
							}}
						>
							ZenStream
						</span>
					</div>
					<div style={{ marginBottom: 28 }}>
						<h1
							style={{
								margin: "0 0 6px",
								fontSize: 22,
								fontWeight: 700,
								color: "#fff",
								letterSpacing: "-.02em",
								lineHeight: 1.2,
							}}
						>
							Create an account
						</h1>
						<p style={{ margin: 0, fontSize: 13, color: "#444" }}>
							Use your invitation to register.
						</p>
					</div>
					<div
						style={{
							display: "flex",
							flexDirection: "column",
							gap: 10,
							marginBottom: 16,
						}}
					>
						<input
							ref={usernameRef}
							required
							value={username}
							onChange={(event) => setUsername(event.target.value)}
							placeholder="Username"
							autoComplete="username"
							style={inputStyle}
						/>
						<input
							required
							type="password"
							value={password}
							onChange={(event) => setPassword(event.target.value)}
							placeholder="Password"
							autoComplete="new-password"
							style={inputStyle}
						/>
					</div>
					{message && (
						<div
							role="status"
							style={{
								fontSize: 12,
								color: message.startsWith("Account")
									? "var(--primary)"
									: "var(--danger)",
								marginBottom: 12,
							}}
						>
							{message}
						</div>
					)}
					<button
						type="submit"
						disabled={loading}
						style={{
							width: "100%",
							background: "var(--primary)",
							border: "none",
							borderRadius: 9,
							padding: 13,
							fontSize: 14,
							fontWeight: 700,
							color: "#000",
							cursor: loading ? "not-allowed" : "pointer",
							fontFamily: "var(--font-sans)",
							opacity: loading ? 0.7 : 1,
						}}
					>
						{loading ? "Registering…" : "Register"}
					</button>
				</form>
				<p
					style={{
						textAlign: "center",
						marginTop: 20,
						fontSize: 11,
						color: "#2a2a2a",
					}}
				>
					ZenStream Orchestrator · v0.9.4
				</p>
			</div>
		</main>
	);
}
