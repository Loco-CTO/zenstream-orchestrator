"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
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
	IconUsers,
	IconX,
} from "@tabler/icons-react";
import { clearSession, readSession, adminFetch, Session } from "./admin-client";

const links = [
	["Overview", "/web/dashboard", IconChartDonut],
	["Tasks", "/web/dashboard/jobs", IconClock],
	["Libraries", "/web/dashboard/libraries", IconLibrary],
	["Metadata", "/web/dashboard/metadata", IconDatabase],
	["Playback", "/web/dashboard/playback", IconPlayerPlay],
	["Users", "/web/dashboard/users", IconUsers],
	["Administrators", "/web/dashboard/administrators", IconShieldLock],
	["Invites", "/web/dashboard/invites", IconKey],
	["Profile & security", "/web/dashboard/profile", IconAdjustments],
] as const;

export default function AdminShell({
	children,
}: {
	children: React.ReactNode;
}) {
	const router = useRouter();
	const pathname = usePathname();
	const [session, setSession] = useState<Session | null>(null);
	const [open, setOpen] = useState(false);
	useEffect(() => {
		const current = readSession();
		if (!current) router.replace("/web/login");
		else setSession(current);
	}, [router]);
	if (!session)
		return (
			<main className="console-root flex min-h-screen items-center justify-center console-muted">
				Loading console…
			</main>
		);
	async function logout() {
		const current = session;
		if (!current) return;
		await adminFetch("/api/admin/logout", current, { method: "POST" });
		clearSession();
		router.replace("/web/login");
	}
	return (
		<div className="console-root min-h-screen">
			<header className="sticky top-0 z-40 flex h-16 items-center justify-between border-b border-white/5 bg-[#111111]/95 px-5 backdrop-blur-xl md:hidden">
				<button
					onClick={() => setOpen(!open)}
					className="rounded-md p-2 console-muted"
				>
					{open ? <IconX size={20} /> : <IconMenu2 size={20} />}
				</button>
				<span className="console-wordmark text-[11px] font-black">
					ZENSTREAM
				</span>
				<button
					onClick={logout}
					aria-label="Sign out"
					className="console-muted"
				>
					<IconLogout size={18} />
				</button>
			</header>
			<aside
				className={`${open ? "flex" : "hidden"} console-rail fixed inset-y-0 left-0 z-30 w-[70px] flex-col border-r px-2 pt-5 pb-2 md:flex`}
			>
				<Link
					href="/web/dashboard"
					className="flex items-center justify-center pb-3"
				>
					<img
						src="/icons/icon.png"
						alt="ZenStream"
						className="h-10 w-10 rounded-xl"
						onError={(event) => {
							event.currentTarget.onerror = null;
							event.currentTarget.src = "/favicon.ico";
						}}
					/>
				</Link>
				<nav className="mt-6 space-y-0.5">
					{links.map(([label, href, Icon]) => (
						<Link
							key={href}
							href={href}
							onClick={() => setOpen(false)}
							className={`admin-nav-item group relative flex h-11 w-full items-center justify-center rounded-md text-xs transition ${pathname === href ? "console-nav-active font-semibold" : "console-nav-link"}`}
							title={label}
						>
							<Icon size={21} stroke={1.7} />
							<span className="admin-tooltip" role="tooltip">
								{label}
							</span>
						</Link>
					))}
				</nav>
				<div className="mt-auto border-t border-white/5 pt-3">
					<button
						onClick={logout}
						aria-label="Sign out"
						title={`Sign out (${session.username})`}
						className="admin-nav-item group relative flex h-10 w-full items-center justify-center rounded-md console-muted hover:text-white"
					>
						<IconLogout size={21} />
						<span className="admin-tooltip" role="tooltip">
							Sign out
						</span>
					</button>
				</div>
			</aside>
			<main className="min-h-screen md:pl-[70px]">
				<div className="w-full px-5 py-10 sm:px-8 md:px-10 md:py-12">
					{children}
				</div>
			</main>
		</div>
	);
}
