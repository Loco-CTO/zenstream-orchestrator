"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
	IconAdjustments,
	IconChartDonut,
	IconCalendar,
	IconClock,
	IconDatabase,
	IconDeviceDesktop,
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
	["Dashboard", "/web/dashboard", IconChartDonut],
	["Tasks", "/web/dashboard/jobs", IconClock],
	["Libraries", "/web/dashboard/libraries", IconLibrary],
	["Calendar", "/web/dashboard/calendar", IconCalendar],
	["Metadata", "/web/dashboard/metadata", IconDatabase],
	["Playback", "/web/dashboard/playback", IconPlayerPlay],
	["Intro & Outro", "/web/dashboard/intro-outro", IconSparkles],
	["Users", "/web/dashboard/users", IconUsers],
	["Devices", "/web/dashboard/devices", IconDeviceDesktop],
	["Administrators", "/web/dashboard/administrators", IconShieldLock],
	["Invites", "/web/dashboard/invites", IconKey],
	["Profile", "/web/dashboard/profile", IconAdjustments],
] as const;

function current(pathname: string | null, href: string) {
	return (
		pathname === href ||
		(href !== "/web/dashboard" && !!pathname?.startsWith(href + "/"))
	);
}

function routeMatches(pathname: string | null, route: string) {
	return pathname === route || pathname?.startsWith(`${route}/`) === true;
}

export default function AdminShell({
	children,
}: {
	children: React.ReactNode;
}) {
	const router = useRouter();
	const pathname = usePathname();
	const wideContent =
		routeMatches(pathname, "/web/dashboard/libraries/view") ||
		routeMatches(pathname, "/web/dashboard/libraries/preview") ||
		routeMatches(pathname, "/web/dashboard/devices");
	const [session, setSession] = useState<Session | null>(null);
	const [drawerOpen, setDrawerOpen] = useState(false);
	const trigger = useRef<HTMLButtonElement>(null);
	const drawer = useRef<HTMLElement>(null);

	useEffect(() => {
		const value = readSession();
		if (!value) router.replace("/web/login");
		else setSession(value);
	}, [router]);

	useEffect(() => {
		if (!drawerOpen) return;
		const node = drawer.current;
		const selector =
			"button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled])";
		const focusables = () =>
			Array.from(node?.querySelectorAll<HTMLElement>(selector) || []);
		window.requestAnimationFrame(() => focusables()[0]?.focus());
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.key === "Escape") setDrawerOpen(false);
			if (event.key !== "Tab") return;
			const items = focusables();
			if (!items.length) return;
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
		window.addEventListener("keydown", onKeyDown);
		return () => {
			window.removeEventListener("keydown", onKeyDown);
			trigger.current?.focus();
		};
	}, [drawerOpen]);

	useEffect(() => setDrawerOpen(false), [pathname]);

	if (!session) {
		return (
			<main
				style={{
					minHeight: "100vh",
					background: "#000",
					color: "#666",
					display: "grid",
					placeItems: "center",
				}}
			>
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

	const renderLinks = (mobile = false) =>
		links.map(([label, href, Icon]) => {
			const active = current(pathname, href);
			return (
				<Link
					key={href}
					href={href}
					aria-label={label}
					aria-current={active ? "page" : undefined}
					onClick={() => mobile && setDrawerOpen(false)}
					style={
						mobile
							? {
									display: "flex",
									alignItems: "center",
									gap: 12,
									height: 44,
									padding: "0 14px",
									borderRadius: 8,
									color: active ? "var(--primary)" : "#606060",
									textDecoration: "none",
								}
							: {
									position: "relative",
									display: "flex",
									alignItems: "center",
									justifyContent: "center",
									width: 36,
									height: 36,
									border: "none",
									borderRadius: 8,
									background: active ? "#111" : "none",
									color: active ? "var(--primary)" : "#606060",
									transition: "color 0.15s, background 0.15s",
								}
					}
				>
					<Icon size={16} stroke={1.6} />
					{mobile && <span style={{ fontSize: 13 }}>{label}</span>}
				</Link>
			);
		});

	return (
		<div
			className="dashboard-design"
			style={{ display: "flex", minHeight: "100vh", background: "#000" }}
		>
			<nav
				className="dashboard-rail"
				style={{
					width: 52,
					flexShrink: 0,
					background: "#060606",
					borderRight: "1px solid #111",
					display: "flex",
					flexDirection: "column",
					alignItems: "center",
					padding: "10px 0",
					position: "fixed",
					top: 0,
					left: 0,
					bottom: 0,
					zIndex: 20,
				}}
			>
				<div
					style={{
						width: "100%",
						display: "flex",
						justifyContent: "center",
						paddingBottom: 12,
						borderBottom: "1px solid #111",
						marginBottom: 10,
					}}
				>
					<Link href="/web/dashboard/" aria-label="ZenStream dashboard">
						<img
							src="/icons/icon.png"
							alt="ZenStream"
							style={{ width: 28, height: 28, display: "block" }}
						/>
					</Link>
				</div>
				<div
					style={{
						flex: 1,
						width: "100%",
						display: "flex",
						flexDirection: "column",
						alignItems: "center",
						gap: 4,
					}}
				>
					{renderLinks()}
				</div>
				<div
					style={{
						width: "100%",
						display: "flex",
						justifyContent: "center",
						paddingTop: 10,
						borderTop: "1px solid #111",
					}}
				>
					<button
						title="Sign out"
						aria-label="Sign out"
						onClick={logout}
						style={{
							display: "flex",
							alignItems: "center",
							justifyContent: "center",
							width: 36,
							height: 36,
							border: "none",
							borderRadius: 8,
							background: "none",
							cursor: "pointer",
							color: "#606060",
						}}
					>
						<IconLogout size={16} stroke={1.6} />
					</button>
				</div>
			</nav>
			<header
				className="dashboard-mobile-bar"
				style={{
					display: "none",
					height: 56,
					alignItems: "center",
					justifyContent: "space-between",
					padding: "0 16px",
					background: "#060606",
					borderBottom: "1px solid #111",
				}}
			>
				<button
					ref={trigger}
					onClick={() => setDrawerOpen(true)}
					aria-label="Open dashboard navigation"
					style={{ background: "none", border: 0, color: "#888" }}
				>
					<IconMenu2 size={20} />
				</button>
				<span
					style={{
						color: "var(--primary)",
						fontSize: 11,
						fontWeight: 700,
						letterSpacing: "0.18em",
						textTransform: "uppercase",
					}}
				>
					ZenStream
				</span>
				<button
					onClick={logout}
					aria-label="Sign out"
					style={{ background: "none", border: 0, color: "#888" }}
				>
					<IconLogout size={19} />
				</button>
			</header>
			{drawerOpen && (
				<div style={{ position: "fixed", inset: 0, zIndex: 50 }}>
					<button
						aria-label="Close navigation"
						onClick={() => setDrawerOpen(false)}
						style={{
							position: "absolute",
							inset: 0,
							width: "100%",
							border: 0,
							background: "rgba(0,0,0,.75)",
						}}
					/>
					<aside
						ref={drawer}
						role="dialog"
						aria-modal="true"
						aria-label="Dashboard navigation"
						style={{
							position: "relative",
							display: "flex",
							flexDirection: "column",
							width: "min(20rem, calc(100vw - 3rem))",
							height: "100%",
							background: "#0d0d0d",
							boxShadow: "12px 0 36px rgba(0,0,0,.35)",
						}}
					>
						<div
							style={{
								display: "flex",
								alignItems: "center",
								justifyContent: "space-between",
								padding: 20,
							}}
						>
							<span
								style={{
									color: "var(--primary)",
									fontSize: 11,
									fontWeight: 700,
									letterSpacing: "0.18em",
								}}
							>
								ZenStream
							</span>
							<button
								aria-label="Close navigation"
								onClick={() => setDrawerOpen(false)}
								style={{ background: "none", border: 0, color: "#888" }}
							>
								<IconX size={20} />
							</button>
						</div>
						<nav style={{ padding: "0 12px" }}>{renderLinks(true)}</nav>
						<button
							onClick={logout}
							style={{
								marginTop: "auto",
								display: "flex",
								alignItems: "center",
								gap: 12,
								padding: 16,
								border: 0,
								borderTop: "1px solid #111",
								background: "none",
								color: "#888",
							}}
						>
							<IconLogout size={18} />
							<span>Sign out</span>
						</button>
					</aside>
				</div>
			)}
			<main
				className="dashboard-content"
				style={{
					marginLeft: 52,
					flex: 1,
					padding: "44px 52px",
					maxWidth: wideContent ? "none" : 1080,
				}}
			>
				{children}
			</main>
		</div>
	);
}
