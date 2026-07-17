"use client";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

export default function AdminLogin() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    const response = await fetch("/api/admin/login", { method: "POST", headers: { Username: username, Password: password } });
    const token = response.headers.get("TOKEN");
    if (response.ok && token) {
      localStorage.setItem("zenstream.admin", JSON.stringify({ username, token }));
      router.push("/web/dashboard");
    } else setError("Invalid administrator credentials.");
    setBusy(false);
  }

  return <main className="flex min-h-screen items-center justify-center bg-[radial-gradient(circle_at_top,#251f50_0%,#07070c_55%)] p-6">
    <form onSubmit={submit} className="w-full max-w-md rounded-3xl border border-white/10 bg-black/25 p-8 shadow-2xl shadow-black/50 backdrop-blur-2xl">
      <div className="mb-8"><img src="/assets/icons/icon.png" alt="ZenStream" className="mb-5 h-14 w-14 rounded-2xl" /><p className="text-xs font-bold uppercase tracking-[.28em] text-violet-300/75">ZenStream</p><h1 className="mt-3 text-3xl font-black">Orchestrator console</h1><p className="mt-2 text-sm text-white/45">Administrator access only.</p></div>
      <div className="space-y-4"><input aria-label="Username" required value={username} onChange={e => setUsername(e.target.value)} placeholder="Username" className="h-12 w-full rounded-xl border border-white/10 bg-white/5 px-4 text-white outline-none placeholder:text-white/30 focus:border-violet-300/60" /><input aria-label="Password" required type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Password" className="h-12 w-full rounded-xl border border-white/10 bg-white/5 px-4 text-white outline-none placeholder:text-white/30 focus:border-violet-300/60" /></div>
      {error && <p role="alert" className="mt-4 text-sm text-red-200">{error}</p>}
      <button disabled={busy} className="mt-6 h-12 w-full rounded-xl bg-violet-300 font-semibold text-black transition hover:bg-violet-200 disabled:opacity-50">{busy ? "Signing in…" : "Sign in"}</button>
    </form>
  </main>;
}
