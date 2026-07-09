import "../globals.css";
import React from "react";
import Navbar from "./components/sidebar";
import Head from "next/head";
import type { Metadata } from "next";

/**
 * Metadata for the dashboard layout.
 */
export const metadata: Metadata = {
	title: "ZenStream | Dashboard",
};

/**
 * DashboardLayout component that provides the layout for the dashboard pages.
 * @param children - The child components to render inside the layout.
 * @returns A React element containing the dashboard layout.
 */
export default function DashboardLayout({
	children,
}: {
	children: React.ReactNode;
}) {
	return (
		<html lang="en">
			<Head>
				<link rel="icon" href="../../public/favicon.ico" />
			</Head>
			<body className="bg-schemes-dark-surface-container-lowest flex flex-row h-screen w-full">
				<Navbar />
				<div className="grow ml-20">{children}</div>
			</body>
		</html>
	);
}
