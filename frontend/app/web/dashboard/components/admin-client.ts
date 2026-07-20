export type Session = { username: string; token: string };

export function readSession(): Session | null {
	if (typeof window === "undefined") return null;
	const raw = localStorage.getItem("zenstream.admin");
	return raw ? (JSON.parse(raw) as Session) : null;
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
	return fetch(path, {
		...init,
		headers: {
			...(init.headers || {}),
			Username: session.username,
			TOKEN: session.token,
		},
	});
}
