<script lang="ts">
	import { enhance } from '$app/forms';
	import type { AppSetting } from '$lib/api';
	import type { ActionData, PageData } from './$types';
	import { toaster } from '$lib/toaster';
	import { THEMES, getStoredTheme, applyTheme, type ThemeValue } from '$lib/theme.svelte';

	let { data, form }: { data: PageData; form: ActionData } = $props();

	let theme = $state<ThemeValue>('cerberus');

	$effect(() => {
		// Reflect the already-applied theme (set pre-paint in app.html) in the select.
		theme = getStoredTheme();
	});

	function onThemeChange(value: ThemeValue) {
		theme = value;
		applyTheme(value);
		toaster.create({ type: 'success', title: 'Theme applied' });
	}

	function tzOffsetLabel(tz: string): string {
		const parts = new Intl.DateTimeFormat('en-US', {
			timeZone: tz,
			timeZoneName: 'longOffset',
		}).formatToParts(new Date());
		const offset = parts.find((p) => p.type === 'timeZoneName')?.value ?? 'GMT';
		return offset.replace('GMT', 'UTC');
	}

	const timezones = Intl.supportedValuesOf('timeZone').map((tz) => ({
		value: tz,
		label: `${tz} (${tzOffsetLabel(tz)})`,
	}));

	const SECTIONS: { title: string; keys: string[] }[] = [
		{
			title: 'Audiobookshelf',
			keys: ['audiobookshelf_url', 'audiobookshelf_api_key'],
		},
		{
			title: 'Calibre Web',
			keys: ['cwa_url', 'cwa_username', 'cwa_password'],
		},
		{
			title: 'Application',
			keys: ['timezone', 'sync_interval_seconds', 'sync_abs_idle_seconds', 'sync_min_movement'],
		},
	];

	let settingsByKey = $derived(
		Object.fromEntries((data.settings as AppSetting[]).map((s) => [s.key, s]))
	);

	$effect(() => {
		if (form?.success) {
			toaster.create({ type: 'success', title: 'Setting saved' });
		} else if (form?.cleared) {
			toaster.create({ type: 'success', title: 'Setting cleared' });
		} else if (form?.synced) {
			toaster.create({
				type: form.alreadyRunning ? 'warning' : 'success',
				title: form.alreadyRunning ? 'Sync already running' : 'Sync started',
			});
		} else if (form?.error) {
			toaster.create({ type: 'error', title: form.error });
		}
	});
</script>

<div class="container mx-auto max-w-3xl space-y-8 p-6">
	<h1 class="h2">Settings</h1>

	{#if data.loadError}
		<aside class="alert preset-filled-error-500"><p>{data.loadError}</p></aside>
	{/if}

	<div class="card bg-surface-100-900 space-y-6 p-6">
		<h2 class="h3">Appearance</h2>
		<div class="space-y-2">
			<label class="font-medium" for="input-theme">Theme</label>
			<p class="text-surface-600-400 text-sm">
				Choose a color palette for the app. Saved in this browser; defaults to your system
				light/dark setting.
			</p>
			<select
				id="input-theme"
				class="select w-full"
				value={theme}
				onchange={(e) => onThemeChange(e.currentTarget.value as ThemeValue)}
			>
				{#each THEMES as t}
					<option value={t.value}>{t.label}</option>
				{/each}
			</select>
		</div>
	</div>

	{#each SECTIONS as section}
		{@const sectionSettings = section.keys.map((k) => settingsByKey[k]).filter(Boolean)}
		{#if sectionSettings.length}
			<div class="card bg-surface-100-900 space-y-6 p-6">
				<h2 class="h3">{section.title}</h2>

				{#each sectionSettings as setting (setting.key)}
					<div class="space-y-2">
						<div class="flex items-center gap-2">
							<label class="font-medium" for="input-{setting.key}">{setting.label}</label>
							{#if setting.is_secret && setting.has_db_value}
								<span class="badge preset-filled-success-500 text-xs">Currently set</span>
							{/if}
						</div>
						<p class="text-surface-600-400 text-sm">{setting.description}</p>

						<div class="flex gap-2">
							<form
								method="POST"
								action="?/update"
								class="flex flex-1 gap-2"
								use:enhance
							>
								<input type="hidden" name="key" value={setting.key} />
								{#if setting.value_type === 'timezone'}
									<select
										id="input-{setting.key}"
										name="value"
										value={setting.display_value}
										class="select flex-1"
									>
										{#if setting.display_value && !timezones.some((tz) => tz.value === setting.display_value)}
											<option value={setting.display_value}>{setting.display_value}</option>
										{/if}
										{#each timezones as tz}
											<option value={tz.value}>{tz.label}</option>
										{/each}
									</select>
								{:else if setting.value_type === 'int'}
									<input
										id="input-{setting.key}"
										type="number"
										min="1"
										step="1"
										name="value"
										value={setting.display_value}
										class="input flex-1"
									/>
								{:else if setting.value_type === 'float'}
									<input
										id="input-{setting.key}"
										type="number"
										min="0"
										max="1"
										step="0.001"
										name="value"
										value={setting.display_value}
										class="input flex-1"
									/>
								{:else}
									<input
										id="input-{setting.key}"
										type={setting.is_secret ? 'password' : 'text'}
										name="value"
										value={setting.is_secret ? '' : setting.display_value}
										placeholder={setting.is_secret ? '••••••••' : ''}
										autocomplete="off"
										class="input flex-1"
									/>
								{/if}
								<button type="submit" class="btn preset-tonal">Save</button>
							</form>

							{#if setting.has_db_value}
								<form method="POST" action="?/clear" use:enhance>
									<input type="hidden" name="key" value={setting.key} />
									<button type="submit" class="btn preset-tonal-error">Clear</button>
								</form>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/each}

	<div class="card bg-surface-100-900 space-y-6 p-6">
		<h2 class="h3">Manual Sync</h2>
		<p class="text-surface-600-400 text-sm">
			Run an ABS ↔ KOSync progress sync now instead of waiting for the next scheduled cycle.
		</p>
		<form method="POST" action="?/syncNow" class="space-y-3" use:enhance>
			<label class="flex items-center gap-2">
				<input type="checkbox" class="checkbox" name="ignore_abs_playing" />
				<span>Ignore ABS playing</span>
			</label>
			<p class="text-surface-600-400 text-sm">
				Sync ABS→KOSync even if ABS updated recently (bypasses the idle guard).
			</p>
			<button type="submit" class="btn preset-filled">Sync now</button>
		</form>
	</div>
</div>
