"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
	IconAdjustments,
	IconChartDonut,
	IconClock,
	IconDatabase,
	IconKey,
	IconLibrary,
	IconLogout,
	IconMenu2,
	IconPlayerPlay,
	IconShieldLock,
	IconSparkles,
	IconUsers,
	IconX,
} from "@tabler/icons-react";
import { adminFetch, clearSession, readSession, Session } from "./admin-client";

const links = [
	["Overview", "/web/dashboard", IconChartDonut],
	["Tasks", "/web/dashboard/jobs", IconClock],
	["Libraries", "/web/dashboard/libraries", IconLibrary],
	["Metadata", "/web/dashboard/metadata", IconDatabase],
	["Playback", "/web/dashboard/playback", IconPlayerPlay],
	["Intro & Outro", "/web/dashboard/intro-outro", IconSparkles],
	["Users", "/web/dashboard/users", IconUsers],
	["Administrators", "/web/dashboard/administrators", IconShieldLock],
	["Invites", "/web/dashboard/invites", IconKey],
	["Profile & security", "/web/dashboard/profile", IconAdjustments],
] as const;

function isCurrentPath(pathname: string | null, href: string) {
	return (
		pathname === href ||
		(href !== "/web/dashboard" && pathname?.startsWith(`${href}/`))
	);
}

function currentLabel(pathname: string | null) {
	return (
		links.find(([, href]) => isCurrentPath(pathname, href))?.[0] ?? "Dashboard"
	);
}

export default function AdminShell({
	children,
}: {
	children: React.ReactNode;
}) {
	const router = useRouter();
	const pathname = usePathname();
	const [session, setSession] = useState<Session | null>(null);
	const [drawerOpen, setDrawerOpen] = useState(false);
	const menuTriggerRef = useRef<HTMLButtonElement>(null);
	const drawerRef = useRef<HTMLElement>(null);
	const pageLabel = currentLabel(pathname);

	useEffect(() => {
		const current = readSession();
		if (!current) router.replace("/web/login");
		else setSession(current);
	}, [router]);

	useEffect(() => {
		if (!drawerOpen) return;
		const drawer = drawerRef.current;
		const focusableSelector =
			'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
		const focusable = () =>
			Array.from(drawer?.querySelectorAll<HTMLElement>(focusableSelector) ?? []);
		const firstFocusable = focusable()[0];
		window.requestAnimationFrame(() => firstFocusable?.focus());

		const keepFocusInDrawer = (event: KeyboardEvent) => {
			if (event.key === "Escape") {
				setDrawerOpen(false);
				return;
			}
			if (event.key !== "Tab") return;
			const items = focusable();
			if (!items.length) {
				event.preventDefault();
				return;
			}
			const first = items[0];
			const last = items[items.length - 1];
			if (event.shiftKey && document.activeElement === first) {
				event.preventDefault();
				last.focus();
			} else if (!event.shiftKey && document.activeElement === last) {
				event.preventDefault();
				first.focus();
			}
		};
		const menuTrigger = menuTriggerRef.current;
		window.addEventListener("keydown", keepFocusInDrawer);
		return () => {
			window.removeEventListener("keydown", keepFocusInDrawer);
			menuTrigger?.focus();
		};
	}, [drawerOpen]);

	useEffect(() => setDrawerOpen(false), [pathname]);

	if (!session) {
		return (
			<main className="console-root flex min-h-screen items-center justify-center console-muted">
				Loading console…
			</main>
		);
	}

	async function logout() {
		if (!session) return;
		await adminFetch("/api/admin/logout", session, { method: "POST" });
		clearSession();
		router.replace("/web/login");
	}

	return (
		<div className="console-root dashboard-shell min-h-screen">
			<aside className="dashboard-rail fixed inset-y-0 left-0 z-40 hidden w-[48px] flex-col px-1.5 py-3 md:flex">
				<Brand compact />
				<nav aria-label="Dashboard" className="mt-6 space-y-1">
					{links.map(([label, href, Icon]) => (
						<Link
							key={href}
							href={href}
							aria-label={label}
							aria-current={isCurrentPath(pathname, href) ? "page" : undefined}
							className={`admin-nav-item group relative flex h-11 w-full items-center justify-center rounded-xl transition ${isCurrentPath(pathname, href) ? "console-nav-active" : "console-nav-link"}`}
						>
							<Icon size={21} stroke={1.8} />
							<span className="admin-tooltip" role="tooltip">
								{label}
							</span>
						</Link>
					))}
				</nav>
				<div className="mt-auto border-t console-divider pt-3">
					<button
						onClick={logout}
						aria-label="Sign out"
						className="admin-nav-item group relative flex h-11 w-full items-center justify-center rounded-xl console-muted transition hover:text-white"
					>
						<IconLogout size={21} stroke={1.8} />
						<span className="admin-tooltip" role="tooltip">
							Sign out
						</span>
					</button>
				</div>
			</aside>

			<header className="dashboard-mobile-bar sticky top-0 z-30 flex h-16 items-center justify-between px-4 md:hidden">
				<button
					ref={menuTriggerRef}
					onClick={() => setDrawerOpen(true)}
					aria-label="Open dashboard navigation"
					aria-expanded={drawerOpen}
					aria-controls="dashboard-mobile-drawer"
					className="material-icon-button"
				>
					<IconMenu2 size={21} />
				</button>
				<div className="text-center">
					<p className="console-wordmark text-[10px] font-black">ZENSTREAM</p>
					<p className="mt-0.5 text-xs font-semibold">{pageLabel}</p>
				</div>
				<button
					onClick={logout}
					aria-label="Sign out"
					className="material-icon-button"
				>
					<IconLogout size={19} />
				</button>
			</header>

			{drawerOpen && (
				<div className="dashboard-drawer-layer md:hidden">
					<button
						type="button"
						aria-label="Close dashboard navigation"
						className="dashboard-drawer-backdrop"
						onClick={() => setDrawerOpen(false)}
					/>
					<aside
						ref={drawerRef}
						id="dashboard-mobile-drawer"
						role="dialog"
						aria-modal="true"
						aria-label="Dashboard navigation"
						className="dashboard-mobile-drawer"
					>
						<div className="flex items-center justify-between px-5 py-5">
							<Brand />
							<button
								onClick={() => setDrawerOpen(false)}
								aria-label="Close dashboard navigation"
								className="material-icon-button"
							>
								<IconX size={20} />
							</button>
						</div>
						<nav aria-label="Dashboard" className="px-3 pb-4">
							{links.map(([label, href, Icon]) => (
								<Link
									key={href}
									href={href}
									aria-current={isCurrentPath(pathname, href) ? "page" : undefined}
									className={`dashboard-drawer-link ${isCurrentPath(pathname, href) ? "console-nav-active" : "console-nav-link"}`}
								>
									<Icon size={20} stroke={1.8} />
									<span>{label}</span>
								</Link>
							))}
						</nav>
						<div className="mt-auto border-t console-divider p-3">
							<button
								onClick={logout}
								className="dashboard-drawer-link w-full console-muted"
							>
								<IconLogout size={20} stroke={1.8} />
								<span>Sign out</span>
							</button>
						</div>
					</aside>
				</div>
			)}

			<main
				className="dashboard-content"
				style={{ marginLeft: 48, flex: 1, maxWidth: "none", padding: "44px 52px" }}
			>
				{children}
			</main>
		</div>
	);
}

function Brand({ compact = false }: { compact?: boolean }) {
	return (
		<Link
			href="/web/dashboard"
			aria-label="ZenStream dashboard"
			className="flex items-center gap-3"
		>
			<img
				src="/icons/icon.png"
				alt=""
				className={`${compact ? "h-8 w-8 rounded-lg" : "h-10 w-10 rounded-xl"}`}
				onError={(event) => {
					event.currentTarget.onerror = null;
					event.currentTarget.src = "/favicon.ico";
				}}
			/>
			{!compact && (
				<span className="console-wordmark text-xs font-black">ZENSTREAM</span>
			)}
		</Link>
	);
}
