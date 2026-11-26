"use client";

import { useEffect, useRef } from "react";

type Poll = () => void | Promise<void>;
type PollOptions = {
	intervalMs: number;
	enabled?: boolean;
	immediate?: boolean;
	onError?: (error: unknown) => void;
};

/** Poll after the configured delay, without overlapping requests or polling hidden tabs. */
export function useVisiblePolling(
	poll: Poll,
	{ intervalMs, enabled = true, immediate = false, onError }: PollOptions,
) {
	const pollRef = useRef(poll);
	const errorRef = useRef(onError);
	const pollingRef = useRef(false);
	const immediateRunRef = useRef(false);

	useEffect(() => {
		pollRef.current = poll;
	}, [poll]);

	useEffect(() => {
		errorRef.current = onError;
	}, [onError]);

	useEffect(() => {
		if (!enabled || intervalMs <= 0) {
			immediateRunRef.current = false;
			return;
		}

		let timer: number | undefined;
		let disposed = false;

		const clearTimer = () => {
			if (timer !== undefined) {
				window.clearTimeout(timer);
				timer = undefined;
			}
		};

		const schedule = () => {
			if (
				disposed ||
				timer !== undefined ||
				document.visibilityState !== "visible"
			)
				return;
			timer = window.setTimeout(() => void run(), intervalMs);
		};

		const run = async () => {
			timer = undefined;
			if (disposed || document.visibilityState !== "visible") return;
			if (pollingRef.current) {
				schedule();
				return;
			}
			pollingRef.current = true;
			try {
				await pollRef.current();
			} catch (error) {
				errorRef.current?.(error);
			} finally {
				pollingRef.current = false;
				schedule();
			}
		};

		const handleVisibilityChange = () => {
			if (document.visibilityState !== "visible") {
				clearTimer();
				return;
			}
			clearTimer();
			if (pollingRef.current) schedule();
			else void run();
		};

		document.addEventListener("visibilitychange", handleVisibilityChange);
		if (immediate && !immediateRunRef.current) {
			immediateRunRef.current = true;
			void run();
		} else schedule();

		return () => {
			disposed = true;
			clearTimer();
			document.removeEventListener("visibilitychange", handleVisibilityChange);
		};
	}, [enabled, immediate, intervalMs]);
}
