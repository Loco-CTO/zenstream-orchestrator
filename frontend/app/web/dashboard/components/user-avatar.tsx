"use client";

import { useState } from "react";
import { apiUrl } from "../../api-url";

function userImageUrl(userId: string, avatarVersion?: string | null) {
	if (!userId || avatarVersion === null) return null;
	const url = apiUrl(
		`/api/admin/users/${encodeURIComponent(userId)}/avatar`,
	);
	if (avatarVersion) {
		const separator = url.includes("?") ? "&" : "?";
		return `${url}${separator}v=${encodeURIComponent(avatarVersion)}`;
	}
	return url;
}

function userInitial(username?: string | null) {
	return Array.from(username?.trim() ?? "")[0]?.toLocaleUpperCase() ?? "?";
}

export function UserAvatar({
	displayName,
	userId,
	avatarVersion,
	containerClassName =
		"flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-full bg-white/8 ring-1 ring-white/12",
	imageClassName = "h-full w-full object-cover",
	fallbackClassName = "text-sm font-semibold text-white/80",
}: {
	displayName: string;
	userId: string;
	avatarVersion?: string | null;
	containerClassName?: string;
	imageClassName?: string;
	fallbackClassName?: string;
}) {
	const [failedUrl, setFailedUrl] = useState<string | null>(null);
	const imageUrl = userImageUrl(userId, avatarVersion);
	const failed = imageUrl !== null && failedUrl === imageUrl;

	return (
		<div className={containerClassName}>
			{!imageUrl || failed ? (
				<span data-testid="default-user-initial" className={fallbackClassName}>
					{userInitial(displayName)}
				</span>
			) : (
				<img
					src={imageUrl}
					alt=""
					className={imageClassName}
					onError={() => setFailedUrl(imageUrl)}
				/>
			)}
		</div>
	);
}
