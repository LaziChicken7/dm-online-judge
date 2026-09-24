const OMNIJUDGE_API = 'https://omnijudge.id.vn/api/vjudge/auto-sync/';
const VJUDGE_HOST = 'vjudge.net';
const SYNC_DEBOUNCE_MS = 2000;

let debounceTimer = null;

// ─── Core sync logic ────────────────────────────────────────────────────────

async function getVJudgeCookie() {
    return new Promise((resolve) => {
        chrome.cookies.get({ url: 'https://vjudge.net', name: 'JSESSIONID' }, (cookie) => {
            resolve(cookie ? cookie.value : null);
        });
    });
}

async function getOmniJudgeSessionId() {
    return new Promise((resolve) => {
        chrome.cookies.get({ url: 'https://omnijudge.id.vn', name: 'sessionid' }, (cookie) => {
            resolve(cookie ? cookie.value : null);
        });
    });
}

async function syncCookie() {
    const jsessionid = await getVJudgeCookie();
    if (!jsessionid) {
        await chrome.storage.local.set({ status: 'no_vjudge_cookie', username: null, lastSync: null });
        return;
    }

    const sessionid = await getOmniJudgeSessionId();
    if (!sessionid) {
        await chrome.storage.local.set({ status: 'not_logged_in_omnijudge', username: null, lastSync: null });
        return;
    }

    try {
        const resp = await fetch(OMNIJUDGE_API, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',   // sends sessionid cookie
            body: JSON.stringify({ jsessionid }),
        });

        const data = await resp.json();

        if (data.ok) {
            await chrome.storage.local.set({
                status: 'synced',
                username: data.username,
                lastSync: new Date().toISOString(),
            });
            console.log('[OmniJudge] VJudge cookie synced:', data.username);
        } else {
            await chrome.storage.local.set({
                status: 'error',
                error: data.error || 'unknown',
                username: null,
                lastSync: new Date().toISOString(),
            });
            console.warn('[OmniJudge] Sync failed:', data.error);
        }
    } catch (e) {
        await chrome.storage.local.set({ status: 'network_error', error: e.message, username: null });
        console.error('[OmniJudge] Network error during sync:', e);
    }
}

function debouncedSync() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(syncCookie, SYNC_DEBOUNCE_MS);
}

// ─── Cookie change listener ──────────────────────────────────────────────────

chrome.cookies.onChanged.addListener((changeInfo) => {
    const { cookie, removed } = changeInfo;
    if (cookie.domain.includes(VJUDGE_HOST) && cookie.name === 'JSESSIONID') {
        if (removed) {
            chrome.storage.local.set({ status: 'vjudge_logged_out', username: null });
        } else {
            debouncedSync();
        }
    }
    // Also sync when user logs into OmniJudge
    if (cookie.domain.includes('omnijudge.id.vn') && cookie.name === 'sessionid' && !removed) {
        debouncedSync();
    }
});

// ─── On install / startup: sync immediately ──────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
    syncCookie();
});

chrome.runtime.onStartup.addListener(() => {
    syncCookie();
});

// ─── External trigger from popup ─────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.action === 'sync_now') {
        syncCookie().then(() => sendResponse({ ok: true }));
        return true; // keep channel open for async response
    }
});
