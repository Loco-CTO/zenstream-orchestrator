import "../../globals.css";
import React from "react";
import Head from "next/head";
import type { Metadata } from "next";

/**
 * Metadata for the not found layout.
 */
export const metadata: Metadata = {
	title: "ZenStream | Not Found",
};

/**
 * NotFoundLayout component that provides the layout for the not found pages.
 * @param children - The child components to render inside the layout.
 * @returns A React element containing the not found layout.
 */
export default function NotFoundLayout({
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
				{children}
			</body>
		</html>
	);
}
