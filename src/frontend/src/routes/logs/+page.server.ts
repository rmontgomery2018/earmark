import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';
import type { LogFileInfo, LogList } from '$lib/api';
import { config } from '$lib/server/config';

const BACKEND = config.backendUrl;

const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

export const load: PageServerLoad = async ({ cookies, url }) => {
	const token = cookies.get('earmark_session');
	if (!token) redirect(302, '/login');

	const file = url.searchParams.get('file') ?? '';
	const levelParam = url.searchParams.get('level') ?? '';
	const level = LEVELS.includes(levelParam) ? levelParam : '';
	const q = url.searchParams.get('q') ?? '';
	const from = url.searchParams.get('from') ?? '';
	const to = url.searchParams.get('to') ?? '';
	const pageNum = Number(url.searchParams.get('page') ?? 1);
	const page = Number.isInteger(pageNum) && pageNum >= 1 ? pageNum : 1;

	const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };

	const empty: LogList = { data: [], total: 0, page, per_page: 100 };

	try {
		const filesRes = await fetch(`${BACKEND}/web/logs/files`, { headers });
		const files: LogFileInfo[] = filesRes.ok ? await filesRes.json() : [];

		const params = new URLSearchParams({ page: String(page) });
		if (file) params.set('file', file);
		if (level) params.set('level', level);
		if (q) params.set('q', q);
		if (from) params.set('from', toIso(from));
		if (to) params.set('to', toIso(to));

		const logsRes = await fetch(`${BACKEND}/web/logs?${params}`, { headers });
		if (!logsRes.ok) {
			return { files, logs: empty, file, level, q, from, to, page, loadError: 'Failed to load logs' };
		}
		const logs: LogList = await logsRes.json();
		return { files, logs, file, level, q, from, to, page, loadError: null };
	} catch {
		return { files: [], logs: empty, file, level, q, from, to, page, loadError: 'Failed to load logs' };
	}
};

// The datetime-local input gives "YYYY-MM-DDTHH:mm" (no zone). Treat it as UTC for the backend.
function toIso(value: string): string {
	return value.length === 16 ? `${value}:00Z` : value;
}
