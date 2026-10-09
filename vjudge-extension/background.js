const OMNIJUDGE_APIS = [
    'https://omnijudge.id.vn/api/vjudge/auto-sync/',
    'http://192.168.218.128/api/vjudge/auto-sync/'
];
const VJUDGE_HOST = 'vjudge.net';
const SYNC_DEBOUNCE_MS = 1500;
let debounceTimer = null;

// ─── Multi-Cookie JSESSIONID Detector (v1.0.1) ───────────────────────────────
async function detectVJudgeCookies() {
    return new Promise((resolve) => {
        chrome.cookies.getAll({ domain: VJUDGE_HOST }, (cookies) => {
            if (!cookies || cookies.length === 0) {
                resolve({ cookieString: null, details: [] });
                return;
            }
            // VJudge sets multiple obfuscated variants: JSESSIONID, JSESSIONlD, JSESSlONID, jax_at
            const sessionCookies = cookies.filter(c => 
                c.name.toLowerCase().includes('jsession') || 
                c.name.toLowerCase().includes('session') ||
                c.name === 'jax_at'
            );
            const targetCookies = sessionCookies.length > 0 ? sessionCookies : cookies;
            const parts = targetCookies.map(c => `${c.name}=${c.value.replace(/\|/g, '%7C')}`);
            const cookieString = parts.join('; ');
            const details = targetCookies.map(c => ({ name: c.name, len: c.value.length }));
            resolve({ cookieString, details });
        });
    });
}

async function getOmniJudgeSessionId(targetHost) {
    return new Promise((resolve) => {
        chrome.cookies.get({ url: targetHost, name: 'sessionid' }, (cookie) => {
            resolve(cookie ? cookie.value : null);
        });
    });
}

async function syncCookie() {
    const { cookieString, details } = await detectVJudgeCookies();
    if (!cookieString) {
        await chrome.storage.local.set({
            status: 'no_vjudge_cookie',
            username: null,
            lastSync: null,
            detectedDetails: []
        });
        return { ok: false, reason: 'no_vjudge_cookie' };
    }

    let syncSuccess = false;
    let syncedUsername = null;
    let syncError = null;

    for (const api of OMNIJUDGE_APIS) {
        const origin = new URL(api).origin;
        const sessionid = await getOmniJudgeSessionId(origin);
        if (!sessionid) continue;

        try {
            const resp = await fetch(api, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ jsessionid: cookieString }),
            });
            const data = await resp.json();
            if (data.ok) {
                syncSuccess = true;
                syncedUsername = data.username;
                break;
            } else {
                syncError = data.error || 'sync_failed';
            }
        } catch (e) {
            syncError = e.message;
        }
    }

    if (syncSuccess) {
        await chrome.storage.local.set({
            status: 'synced',
            username: syncedUsername,
            lastSync: new Date().toISOString(),
            detectedDetails: details,
            cookieCount: details.length
        });
        console.log('[OmniJudge v1.0.1] VJudge cookies synced successfully:', syncedUsername);
        return { ok: true, username: syncedUsername, details };
    } else {
        await chrome.storage.local.set({
            status: syncError ? 'error' : 'not_logged_in_omnijudge',
            error: syncError,
            username: null,
            detectedDetails: details,
            cookieCount: details.length,
            lastSync: new Date().toISOString(),
        });
        return { ok: false, error: syncError, details };
    }
}

function debouncedSync() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(syncCookie, SYNC_DEBOUNCE_MS);
}

// ─── Listeners ──────────────────────────────────────────────────────────────
chrome.cookies.onChanged.addListener((changeInfo) => {
    const { cookie, removed } = changeInfo;
    if (cookie.domain.includes(VJUDGE_HOST)) {
        if (removed && cookie.name.toLowerCase().includes('jsession')) {
            chrome.storage.local.set({ status: 'vjudge_logged_out', username: null });
        } else {
            debouncedSync();
        }
    }
    if ((cookie.domain.includes('omnijudge.id.vn') || cookie.domain.includes('192.168.218.128')) && cookie.name === 'sessionid' && !removed) {
        debouncedSync();
    }
});

chrome.runtime.onInstalled.addListener(() => {
    syncCookie();
});
chrome.runtime.onStartup.addListener(() => {
    syncCookie();
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.action === 'sync_now') {
        syncCookie().then((res) => sendResponse(res));
        return true;
    }
    if (msg.action === 'detect_now') {
        detectVJudgeCookies().then((res) => sendResponse(res));
        return true;
    }
});
