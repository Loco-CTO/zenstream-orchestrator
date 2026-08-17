const configuredApiUrl = process.env.NEXT_PUBLIC_ORCHESTRATOR_API_URL?.trim();

function isLoopback(hostname: string) {
	return (
		hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]"
	);
}

export function apiUrl(path: string) {
	if (!configuredApiUrl) return path;
	const base = new URL(configuredApiUrl);
	// Keep loopback development requests same-site even when Next and the
	// Orchestrator were configured with different localhost spellings.
	if (
		typeof window !== "undefined" &&
		isLoopback(base.hostname) &&
		isLoopback(window.location.hostname)
	) {
		base.hostname = window.location.hostname;
	}
	return new URL(path, base).toString();
}
