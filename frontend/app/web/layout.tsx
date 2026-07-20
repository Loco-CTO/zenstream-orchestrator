import "../globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
	title: "ZenStream | Orchestrator",
	icons: { icon: "/favicon.ico" },
};

export default function WebLayout({ children }: { children: React.ReactNode }) {
	return (
		<html lang="en">
			<body className="min-h-screen bg-[#07070c] text-white">{children}</body>
		</html>
	);
}
