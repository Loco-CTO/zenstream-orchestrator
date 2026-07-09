import "../../../globals.css";
import React from "react";
import Head from "next/head";
import type { Metadata } from "next";

/**
 * Metadata for the register layout.
 */
export const metadata: Metadata = {
	title: "ZenStream | Register",
};

/**
 * RegisterLayout component that provides the layout for the register pages.
 * @param children - The child components to render inside the layout.
 * @returns A React element containing the register layout.
 */
export default function RegisterLayout({
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
