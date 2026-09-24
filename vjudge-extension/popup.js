const STATUS_MESSAGES = {
    synced:                 (s) => ({ cls: 'synced',  title: `✅ Đã kết nối: ${s.username}`, sub: `Lần cuối: ${fmtDate(s.lastSync)}` }),
    error:                  (s) => ({ cls: 'error',   title: '⚠️ Lỗi đồng bộ', sub: s.error || 'Thử lại hoặc đăng nhập lại VJudge' }),
    network_error:          (s) => ({ cls: 'error',   title: '⚠️ Lỗi mạng', sub: 'Kiểm tra kết nối internet' }),
    no_vjudge_cookie:       ()  => ({ cls: 'waiting', title: '🔑 Chưa đăng nhập VJudge', sub: 'Hãy đăng nhập vào vjudge.net trên trình duyệt này' }),
    not_logged_in_omnijudge:()  => ({ cls: 'waiting', title: '🔒 Chưa đăng nhập OmniJudge', sub: 'Hãy đăng nhập tại omnijudge.id.vn' }),
    vjudge_logged_out:      ()  => ({ cls: 'error',   title: '🔴 VJudge đã đăng xuất', sub: 'Đăng nhập lại vjudge.net để đồng bộ' }),
};

function fmtDate(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('vi-VN', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' });
    } catch { return iso; }
}

function renderStatus(state) {
    const card = document.getElementById('status-card');
    const titleEl = document.getElementById('status-title');
    const subEl = document.getElementById('status-sub');

    const handler = STATUS_MESSAGES[state.status] || (() => ({ cls: 'waiting', title: 'Chưa đồng bộ', sub: 'Bấm "Đồng bộ ngay"' }));
    const { cls, title, sub } = handler(state);

    card.className = `status-card ${cls}`;
    titleEl.textContent = title;
    subEl.textContent = sub || '';
}

// Load current state from storage
chrome.storage.local.get(['status', 'username', 'lastSync', 'error'], renderStatus);

// Sync now button
document.getElementById('btn-sync').addEventListener('click', async () => {
    const btn = document.getElementById('btn-sync');
    btn.disabled = true;
    btn.textContent = '⏳ Đang đồng bộ…';

    chrome.runtime.sendMessage({ action: 'sync_now' }, () => {
        chrome.storage.local.get(['status', 'username', 'lastSync', 'error'], (state) => {
            renderStatus(state);
            btn.disabled = false;
            btn.textContent = '🔄 Đồng bộ ngay';
        });
    });
});

// Open OmniJudge connect page
document.getElementById('btn-open-connect').addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://omnijudge.id.vn/user/vjudge/connect/' });
});
