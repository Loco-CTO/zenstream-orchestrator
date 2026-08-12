"use client";

import { ReactNode, useEffect, useRef } from "react";

export function PageHeader({
	title,
	description,
	actions,
}: {
	title: string;
	description?: string;
	actions?: ReactNode;
}) {
	return (
		<header
			style={{
				display: "flex",
				alignItems: "flex-start",
				justifyContent: "space-between",
				marginBottom: 36,
				gap: 16,
				flexWrap: "wrap",
			}}
		>
			<div style={{ maxWidth: "70%" }}>
				<h1
					style={{
						margin: 0,
						fontSize: 22,
						fontWeight: 600,
						color: "#fff",
						letterSpacing: "-0.02em",
					}}
				>
					{title}
				</h1>
				{description && (
					<p
						style={{
							margin: "5px 0 0",
							fontSize: 13,
							color: "#666",
							lineHeight: 1.5,
						}}
					>
						{description}
					</p>
				)}
			</div>
			{actions && (
				<div
					style={{ display: "flex", gap: 8, flexShrink: 0, alignItems: "center" }}
				>
					{actions}
				</div>
			)}
		</header>
	);
}

export function SurfaceCard({
	children,
	className = "",
}: {
	children: ReactNode;
	className?: string;
}) {
	return (
		<section
			className={`console-card ${className}`}
			style={{ background: "#080808", borderRadius: 12, padding: "20px 22px" }}
		>
			{children}
		</section>
	);
}

export function SectionHeader({
	kicker,
	title,
	action,
}: {
	kicker?: string;
	title: string;
	action?: ReactNode;
}) {
	return (
		<div
			style={{
				display: "flex",
				alignItems: "flex-start",
				justifyContent: "space-between",
				gap: 16,
				borderBottom: "1px solid #111",
				padding: "16px 20px",
			}}
		>
			<div>
				{kicker && (
					<p
						style={{
							fontSize: 10,
							fontWeight: 600,
							letterSpacing: "0.1em",
							textTransform: "uppercase",
							color: "var(--primary)",
							marginBottom: 8,
						}}
					>
						{kicker}
					</p>
				)}
				<h2
					style={{
						margin: 0,
						fontSize: kicker ? 16 : 13,
						fontWeight: 600,
						color: "#fff",
					}}
				>
					{title}
				</h2>
			</div>
			{action}
		</div>
	);
}

export function StatusMessage({ children }: { children: ReactNode }) {
	return (
		<p
			style={{
				marginTop: 20,
				border: "1px solid rgba(94,227,216,.2)",
				background: "rgba(94,227,216,.1)",
				borderRadius: 8,
				padding: "10px 14px",
				fontSize: 13,
				color: "#d8fffb",
			}}
		>
			{children}
		</p>
	);
}

export function EmptyState({ children }: { children: ReactNode }) {
	return (
		<div
			style={{
				padding: "42px 20px",
				textAlign: "center",
				fontSize: 13,
				color: "#666",
			}}
		>
			{children}
		</div>
	);
}

export function ConfirmDialog({
	open,
	title,
	description,
	confirmLabel,
	destructive = false,
	busy = false,
	onClose,
	onConfirm,
}: {
	open: boolean;
	title: string;
	description: string;
	confirmLabel: string;
	destructive?: boolean;
	busy?: boolean;
	onClose: () => void;
	onConfirm: () => void;
}) {
	const cancelRef = useRef<HTMLButtonElement>(null);

	useEffect(() => {
		if (!open) return;
		cancelRef.current?.focus();
		const closeOnEscape = (event: KeyboardEvent) => {
			if (event.key === "Escape" && !busy) onClose();
		};
		window.addEventListener("keydown", closeOnEscape);
		return () => window.removeEventListener("keydown", closeOnEscape);
	}, [busy, onClose, open]);

	if (!open) return null;
	return (
		<div className="dashboard-dialog-layer" role="presentation">
			<button
				type="button"
				className="dashboard-dialog-backdrop"
				aria-label="Close dialog"
				disabled={busy}
				onClick={onClose}
			/>
			<section
				role="dialog"
				aria-modal="true"
				aria-labelledby="dashboard-dialog-title"
				aria-describedby="dashboard-dialog-description"
				className="dashboard-dialog"
			>
				<p className="console-kicker">Confirmation required</p>
				<h2 id="dashboard-dialog-title" className="mt-2 text-xl font-semibold">
					{title}
				</h2>
				<p
					id="dashboard-dialog-description"
					className="mt-3 text-sm leading-6 console-muted"
				>
					{description}
				</p>
				<div className="mt-7 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
					<button
						ref={cancelRef}
						type="button"
						disabled={busy}
						onClick={onClose}
						className="material-icon-button h-11 px-4 text-sm font-semibold"
					>
						Cancel
					</button>
					<button
						type="button"
						disabled={busy}
						onClick={onConfirm}
						className={`${destructive ? "dashboard-danger-button" : "console-button"} h-11 rounded-xl px-4 text-sm font-semibold disabled:opacity-60`}
					>
						{busy ? "Working…" : confirmLabel}
					</button>
				</div>
			</section>
		</div>
	);
}
