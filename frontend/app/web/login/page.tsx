"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { saveSession } from "../dashboard/components/admin-client";
import { apiUrl } from "../api-url";

export default function AdminLogin() {
	const router = useRouter();
	const [username, setUsername] = useState("");
	const [password, setPassword] = useState("");
	const [error, setError] = useState("");
	const [loading, setLoading] = useState(false);
	const usernameRef = useRef<HTMLInputElement>(null);

	useEffect(() => usernameRef.current?.focus(), []);

	async function submit(event?: FormEvent) {
		event?.preventDefault();
		if (!username.trim() || !password.trim()) {
			setError("Enter your username and password.");
			return;
		}
		setError("");
		setLoading(true);
		try {
			const response = await fetch(apiUrl("/api/admin/login"), {
				method: "POST",
				credentials: "include",
				headers: { Username: username, Password: password },
			});
			if (!response.ok) {
				setError("Invalid administrator credentials.");
				return;
			}
			const profile = (await response.json()) as { username: string };
			saveSession({ username: profile.username });
			router.push("/web/dashboard/");
		} catch {
			setError("Unable to reach the Orchestrator.");
		} finally {
			setLoading(false);
		}
	}

	const inputStyle: React.CSSProperties = {
		width: "100%",
		background: "#0a0a0a",
		border: "1px solid #1c1c1c",
		borderRadius: 9,
		padding: "13px 16px",
		color: "#ddd",
		fontSize: 14,
		fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
		transition: "border-color 0.15s",
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
								letterSpacing: "0.18em",
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
								letterSpacing: "-0.02em",
								lineHeight: 1.2,
							}}
						>
							Orchestrator console
						</h1>
						<p style={{ margin: 0, fontSize: 13, color: "#444" }}>
							Administrator access only.
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
							value={username}
							onChange={(e) => setUsername(e.target.value)}
							placeholder="Username"
							autoComplete="username"
							onFocus={(e) => (e.target.style.borderColor = "var(--primary)")}
							onBlur={(e) => (e.target.style.borderColor = "#1c1c1c")}
							style={inputStyle}
						/>
						<input
							type="password"
							value={password}
							onChange={(e) => setPassword(e.target.value)}
							placeholder="Password"
							autoComplete="current-password"
							onFocus={(e) => (e.target.style.borderColor = "var(--primary)")}
							onBlur={(e) => (e.target.style.borderColor = "#1c1c1c")}
							style={inputStyle}
						/>
					</div>
					{error && (
						<div
							role="alert"
							style={{ fontSize: 12, color: "var(--danger)", marginBottom: 12 }}
						>
							{error}
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
							padding: "13px",
							fontSize: 14,
							fontWeight: 700,
							color: "#000",
							cursor: loading ? "not-allowed" : "pointer",
							fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
							letterSpacing: "0.02em",
							opacity: loading ? 0.7 : 1,
							transition: "filter 0.15s, opacity 0.15s",
						}}
					>
						{loading ? "Signing in…" : "Sign in"}
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
