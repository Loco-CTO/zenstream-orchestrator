"use client";

import { useEffect } from "react";
import { apiUrl } from "../api-url";

export default function LegacyRegisterRedirect() {
	useEffect(() => {
		const query = window.location.search;
		void fetch(apiUrl("/api/config/public-web-url"), {
			credentials: "include",
		})
			.then((response) => (response.ok ? response.json() : null))
			.then((payload: { publicWebUrl?: string } | null) => {
				const target = (payload?.publicWebUrl || "").replace(/\/+$/, "");
				if (target) window.location.replace(`${target}/register${query}`);
			});
	}, []);

	return (
		<main className="flex min-h-screen items-center justify-center bg-[#050505] px-6 text-sm text-white/60">
			Redirecting to the ZenStream web registration page…
		</main>
	);
}
