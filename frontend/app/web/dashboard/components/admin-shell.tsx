"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { clearSession, readSession, adminFetch, Session } from "./admin-client";

const links = [
  ["Overview", "/web/dashboard"], ["Users", "/web/dashboard/users"], ["Administrators", "/web/dashboard/administrators"], ["Invites", "/web/dashboard/invites"], ["Profile & security", "/web/dashboard/profile"],
];

export default function AdminShell({ children }: { children: React.ReactNode }) {
  const router = useRouter(); const pathname = usePathname(); const [session, setSession] = useState<Session | null>(null); const [open, setOpen] = useState(false);
  useEffect(() => { const current = readSession(); if (!current) router.replace("/web/login"); else setSession(current); }, [router]);
  if (!session) return <main className="flex min-h-screen items-center justify-center text-white/55">Loading console…</main>;
  async function logout() { if (!session) return; await adminFetch("/api/admin/logout", session, { method: "POST" }); clearSession(); router.replace("/web/login"); }
  return <div className="min-h-screen bg-[radial-gradient(circle_at_top_right,#1d2046_0%,#07070c_48%)] text-white"><header className="flex h-18 items-center justify-between border-b border-white/10 bg-black/30 px-5 backdrop-blur-xl md:hidden"><button onClick={() => setOpen(!open)} className="rounded-lg border border-white/10 px-3 py-2 text-sm">Menu</button><span className="text-sm font-semibold">Orchestrator</span><button onClick={logout} className="text-sm text-white/55">Sign out</button></header><aside className={`fixed inset-y-0 left-0 z-30 w-64 border-r border-white/10 bg-[#090911]/95 p-5 backdrop-blur-2xl ${open ? "block" : "hidden"} md:block`}><div className="flex items-center gap-3 border-b border-white/10 pb-6"><img src="/assets/icons/icon.png" alt="ZenStream" className="h-11 w-11 rounded-xl" /><div><p className="text-xs font-bold uppercase tracking-[.25em] text-violet-300">ZenStream</p><p className="mt-1 text-xs text-white/45">Admin console</p></div></div><nav className="mt-7 space-y-1">{links.map(([label, href]) => <Link key={href} href={href} onClick={() => setOpen(false)} className={`block rounded-xl px-4 py-3 text-sm transition ${pathname === href ? "bg-violet-300 font-semibold text-black" : "text-white/55 hover:bg-white/10 hover:text-white"}`}>{label}</Link>)}</nav><div className="absolute inset-x-5 bottom-5 border-t border-white/10 pt-4"><p className="truncate text-xs text-white/45">Signed in as</p><p className="mt-1 truncate text-sm font-semibold">{session.username}</p><button onClick={logout} className="mt-4 text-sm text-white/55 hover:text-white">Sign out</button></div></aside><main className="md:pl-64"><div className="mx-auto max-w-7xl p-5 md:p-10">{children}</div></main></div>;
}
