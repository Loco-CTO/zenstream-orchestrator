"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
	IconAdjustments,
	IconChartDonut,
	IconDatabase,
	IconKey,
	IconLibrary,
	IconLogout,
	IconMenu2,
	IconShieldLock,
	IconUsers,
	IconX,
} from "@tabler/icons-react";
import { clearSession, readSession, adminFetch, Session } from "./admin-client";

const links = [
	["Overview", "/web/dashboard", IconChartDonut],
	["Libraries", "/web/dashboard/libraries", IconLibrary],
	["Metadata", "/web/dashboard/metadata", IconDatabase],
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
			<header className="sticky top-0 z-40 flex h-16 items-center justify-between border-b border-white/5 bg-[#050505]/95 px-5 backdrop-blur-xl md:hidden">
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
				className={`${open ? "flex" : "hidden"} console-rail fixed inset-y-0 left-0 z-30 w-[270px] flex-col border-r p-6 md:flex`}
			>
				<Link
					href="/web/dashboard"
					className="flex items-center gap-3 border-b border-white/5 pb-7"
				>
					<img
						src="/assets/icons/icon.png"
						alt="ZenStream"
						className="h-11 w-11 rounded-xl"
					/>
					<div>
						<p className="console-wordmark text-[11px] font-black">ZENSTREAM</p>
						<p className="mt-1 text-xs console-muted">Orchestrator control</p>
					</div>
				</Link>
				<div className="mt-6 flex items-center gap-2 text-xs console-muted">
					<span className="h-2 w-2 rounded-full bg-[#8edbc9]" /> Service online
				</div>
				<nav className="mt-7 space-y-1">
					{links.map(([label, href, Icon]) => (
						<Link
							key={href}
							href={href}
							onClick={() => setOpen(false)}
							className={`flex items-center gap-3 rounded-md px-3 py-3 text-sm transition ${pathname === href ? "console-nav-active font-semibold" : "console-nav-link"}`}
						>
							<Icon size={17} stroke={1.7} />
							{label}
						</Link>
					))}
				</nav>
				<div className="mt-auto border-t border-white/5 pt-5">
					<p className="text-[10px] uppercase tracking-[.15em] console-muted">
						Signed in as
					</p>
					<p className="mt-2 text-sm font-semibold">{session.username}</p>
					<button
						onClick={logout}
						className="mt-5 flex items-center gap-2 text-sm console-muted hover:text-white"
					>
						<IconLogout size={16} /> Sign out
					</button>
				</div>
			</aside>
			<main className="min-h-screen md:pl-[270px]">
				<div className="w-full px-5 py-8 sm:px-8 md:px-12 md:py-10">
					{children}
				</div>
			</main>
		</div>
	);
}
