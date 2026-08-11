export type Session = { username: string };

export function readSession(): Session | null {
	if (typeof window === "undefined") return null;
	const raw = localStorage.getItem("zenstream.admin");
	if (!raw) return null;
	try {
		const value = JSON.parse(raw) as Partial<Session>;
		return typeof value.username === "string"
			? { username: value.username }
			: null;
	} catch {
		return null;
	}
}

export function saveSession(session: Session) {
	localStorage.setItem("zenstream.admin", JSON.stringify(session));
}

export function clearSession() {
	localStorage.removeItem("zenstream.admin");
}

export async function adminFetch(
	path: string,
	session: Session,
	init: RequestInit = {},
) {
	const response = await fetch(path, {
		...init,
		credentials: "same-origin",
		headers: init.headers,
	});
	if (response.status === 401 && typeof window !== "undefined") {
		clearSession();
		if (!window.location.pathname.endsWith("/login"))
			window.location.replace("/web/login");
	}
	return response;
}
