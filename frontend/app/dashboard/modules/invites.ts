import { getCookie } from "cookies-next";
import appConfig from "@/app/config";

/**
 * Generates an invite by making a POST request to the server.
 * @returns {Promise<string | undefined>} A promise that resolves to the invite ID if successful, otherwise undefined.
 */
async function generateInvite(): Promise<string | undefined> {
	try {
		const user = getCookie("Username");
		const token = getCookie("TOKEN");

		const response = await fetch(
			`${appConfig.apiUrl}/api/user/generate_invite`,
			{
				method: "POST",
				headers: {
					Username: String(user) || "",
					TOKEN: String(token) || "",
				},
			},
		);

		if (response.ok) {
			const data = await response.json();
			return data.inviteid;
		}
	} catch (error) {
		console.error(error);
	}
	return undefined;
}

const invitesModule = { generateInvite };

export default invitesModule;
