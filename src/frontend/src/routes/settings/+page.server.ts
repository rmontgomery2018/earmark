import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import type { AppSetting } from '$lib/api';
import { config } from '$lib/server/config';

const BACKEND = config.backendUrl;

export const load: PageServerLoad = async ({ cookies }) => {
	const token = cookies.get('earmark_session');
	if (!token) redirect(302, '/login');

	const headers = { Authorization: `Bearer ${token}` };

	try {
		const res = await fetch(`${BACKEND}/web/settings`, { headers });
		if (!res.ok) {
			return { settings: [] as AppSetting[], loadError: 'Failed to load settings' };
		}
		const settings = (await res.json()) as AppSetting[];
		return { settings, loadError: null };
	} catch {
		return { settings: [] as AppSetting[], loadError: 'Failed to load settings' };
	}
};

export const actions: Actions = {
	update: async ({ request, cookies }) => {
		const token = cookies.get('earmark_session');
		if (!token) return fail(401, { error: 'Not authenticated' });

		const formData = await request.formData();
		const key = String(formData.get('key') ?? '');
		const value = String(formData.get('value') ?? '');

		if (!key) return fail(422, { error: 'Missing key' });
		if (!value) return fail(422, { key, error: 'Value must not be empty' });

		const res = await fetch(`${BACKEND}/web/settings/${key}`, {
			method: 'PUT',
			headers: {
				Authorization: `Bearer ${token}`,
				'Content-Type': 'application/json',
			},
			body: JSON.stringify({ value }),
		});

		if (!res.ok) {
			const body = await res.json().catch(() => ({})) as { detail?: string };
			return fail(res.status, { key, error: body.detail ?? 'Failed to save setting' });
		}

		return { key, success: true };
	},

	syncNow: async ({ request, cookies }) => {
		const token = cookies.get('earmark_session');
		if (!token) return fail(401, { error: 'Not authenticated' });

		const formData = await request.formData();
		const ignore = formData.get('ignore_abs_playing') === 'on';

		const res = await fetch(`${BACKEND}/web/sync/run?ignore_abs_playing=${ignore}`, {
			method: 'POST',
			headers: { Authorization: `Bearer ${token}` },
		});

		if (!res.ok) {
			const body = await res.json().catch(() => ({})) as { detail?: string };
			return fail(res.status, { error: body.detail ?? 'Failed to start sync' });
		}

		const body = (await res.json()) as { started: boolean; already_running: boolean };
		return { synced: true, alreadyRunning: body.already_running };
	},

	clear: async ({ request, cookies }) => {
		const token = cookies.get('earmark_session');
		if (!token) return fail(401, { error: 'Not authenticated' });

		const formData = await request.formData();
		const key = String(formData.get('key') ?? '');

		if (!key) return fail(422, { error: 'Missing key' });

		const res = await fetch(`${BACKEND}/web/settings/${key}`, {
			method: 'DELETE',
			headers: { Authorization: `Bearer ${token}` },
		});

		if (!res.ok) {
			const body = await res.json().catch(() => ({})) as { detail?: string };
			return fail(res.status, { key, error: body.detail ?? 'Failed to clear setting' });
		}

		return { key, cleared: true };
	},
};
