"use client";

import { useEffect } from "react";

export default function LegacyRegisterRedirect() {
	useEffect(() => {
		const target = (process.env.NEXT_PUBLIC_ZENSTREAM_WEB_URL || "").replace(
			/\/+$/,
			"",
		);
		const query = window.location.search;
		if (target) window.location.replace(`${target}/register${query}`);
	}, []);

	return (
		<main className="flex min-h-screen items-center justify-center bg-[#050505] px-6 text-sm text-white/60">
			Redirecting to the ZenStream web registration page…
		</main>
	);
}
