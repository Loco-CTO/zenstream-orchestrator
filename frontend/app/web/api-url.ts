const configuredApiUrl = process.env.NEXT_PUBLIC_ORCHESTRATOR_API_URL?.trim();
const isDevelopment = process.env.NODE_ENV === "development";

function isLoopback(hostname: string) {
	return (
		hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]"
	);
}

export function apiUrl(path: string) {
	if (!configuredApiUrl) return path;
	const base = new URL(configuredApiUrl);
	// In LAN development, localhost in the browser points at the browser device.
	// Keep a loopback-configured API on the host that served the dashboard.
	if (
		isDevelopment &&
		typeof window !== "undefined" &&
		isLoopback(base.hostname)
	) {
		base.hostname = window.location.hostname;
	}
	return new URL(path, base).toString();
}
