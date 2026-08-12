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
		<header className="dashboard-page-header flex flex-col gap-4 pb-5 sm:flex-row sm:items-end sm:justify-between">
			<div className="max-w-3xl">
				<h1 className="text-[1.7rem] font-semibold tracking-[-0.03em]">{title}</h1>
				{description && (
					<p className="mt-2 text-sm leading-6 console-muted">{description}</p>
				)}
			</div>
			{actions && (
				<div className="flex flex-wrap items-center gap-3">{actions}</div>
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
		<section className={`console-card rounded-xl ${className}`}>
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
		<div className="flex items-start justify-between gap-4 border-b console-divider px-5 py-4">
			<div>
				{kicker && <p className="console-kicker">{kicker}</p>}
				<h2
					className={kicker ? "mt-1 text-lg font-semibold" : "text-sm font-semibold"}
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
		<p className="mt-5 rounded-xl border border-[#5ee3d8]/20 bg-[#5ee3d8]/10 px-4 py-3 text-sm text-[#d8fffb]">
			{children}
		</p>
	);
}

export function EmptyState({ children }: { children: ReactNode }) {
	return (
		<div className="px-5 py-12 text-center text-sm console-muted">{children}</div>
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
