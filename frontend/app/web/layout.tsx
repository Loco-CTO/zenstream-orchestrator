import "../globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
	title: "ZenStream | Orchestrator",
	icons: { icon: "/favicon.ico" },
};

export default function WebLayout({ children }: { children: React.ReactNode }) {
	return children;
}
