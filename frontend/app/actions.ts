"use server";

import fs from "fs/promises";
import path from "path";

export async function saveConfig(config: unknown) {
	const configPath = path.join(process.cwd(), "datastore", "config.json");
	fs.writeFile(configPath, JSON.stringify(config, null, 2));
	return true;
}
