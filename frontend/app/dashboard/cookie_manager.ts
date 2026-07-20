import { setCookie } from "cookies-next";

/**
 * Sets the cookies for the given username and token.
 *
 * @param {string} username - The username to set in the cookie.
 * @param {string} token - The token to set in the cookie.
 */
const setCookies = (username: string, token: string) => {
	setCookie("Username", username, {
		path: "/",
		sameSite: "lax",
		secure: process.env.NODE_ENV === "production",
	});
	setCookie("TOKEN", token, {
		path: "/",
		sameSite: "lax",
		secure: process.env.NODE_ENV === "production",
	});
};

const cookieManager = { setCookies };

export default cookieManager;
