/**
 * NotFound component renders a 404 error page.
 * It displays a message indicating that the requested page was not found.
 */
"use client";
import React from "react";

/**
 * NotFound functional component.
 * @returns {JSX.Element} A JSX element representing the 404 error page.
 */
export default function NotFound() {
	return (
		<div className="flex w-full h-screen bg-linear-to-br from-schemes-dark-surface-container-lowest to-schemes-dark-medium-contrast-inverse-primary items-center justify-center">
			<div className="h-[292px] w-[388px] bg-clip-border bg-schemes-dark-surface-container-low rounded-xl flex flex-col items-center justify-center overflow-hidden relative shadow-md shadow-schemes-dark-surface-container-lowest">
				<div className="flex h-[600px] w-96 bg-conic rounded-xl from-transparent to-schemes-dark-on-background from-85% items-center justify-center absolute animate-spin-slow" />
				<div className="flex flex-col items-center justify-start h-auto w-96 bg-schemes-dark-surface-container-low rounded-xl relative gap-4 p-6">
					<h1 className="text-schemes-dark-on-surface text-3xl font-bold">
						404 Not Found
					</h1>
					<p className="text-schemes-dark-on-surface text-sm">
						The page you are looking for does not exist.
					</p>
				</div>
			</div>
		</div>
	);
}
