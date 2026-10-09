function renderState(state) {
    const badge = document.getElementById('status-badge');
    const icon = document.getElementById('status-icon');
    const text = document.getElementById('status-text');
    const desc = document.getElementById('status-desc');
    const chips = document.getElementById('cookie-chips');

    badge.className = 'status-badge';

    if (state.status === 'synced') {
        badge.classList.add('synced');
        icon.textContent = '✅';
        text.textContent = `Đã kết nối: ${state.username || 'VJudge'}`;
        desc.textContent = `Đồng bộ thành công! Lần cuối: ${formatDate(state.lastSync)}`;
    } else if (state.status === 'no_vjudge_cookie') {
        badge.classList.add('waiting');
        icon.textContent = '🔑';
        text.textContent = 'Chưa đăng nhập VJudge';
        desc.textContent = 'Vui lòng mở vjudge.net và đăng nhập tài khoản của bạn.';
    } else if (state.status === 'not_logged_in_omnijudge') {
        badge.classList.add('waiting');
        icon.textContent = '🔒';
        text.textContent = 'Chưa đăng nhập OmniJudge';
        desc.textContent = 'Vui lòng đăng nhập tại omnijudge.id.vn để nhận phiên.';
    } else if (state.status === 'vjudge_logged_out') {
        badge.classList.add('error');
        icon.textContent = '🔴';
        text.textContent = 'VJudge đã đăng xuất';
        desc.textContent = 'Phiên đăng nhập trên vjudge.net đã kết thúc.';
    } else {
        badge.classList.add('error');
        icon.textContent = '⚠️';
        text.textContent = 'Lỗi đồng bộ';
        desc.textContent = state.error || 'Vui lòng kiểm tra lại kết nối mạng.';
    }

    if (state.detectedDetails && state.detectedDetails.length > 0) {
        chips.innerHTML = state.detectedDetails.map(d => `<span class="cookie-chip">${d.name} (${d.len}b)</span>`).join('');
    } else {
        chips.innerHTML = '<span style="font-size: 11px; color: #64748b;">Chưa tìm thấy cookie</span>';
    }
}

function formatDate(iso) {
    if (!iso) return 'Vừa xong';
    try {
        return new Date(iso).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return iso; }
}

// Initial load
chrome.storage.local.get(['status', 'username', 'lastSync', 'error', 'detectedDetails'], renderState);

// Trigger live detection
chrome.runtime.sendMessage({ action: 'detect_now' }, (res) => {
    if (res && res.details) {
        chrome.storage.local.get(['status', 'username', 'lastSync', 'error'], (st) => {
            st.detectedDetails = res.details;
            renderState(st);
        });
    }
});

document.getElementById('btn-sync').addEventListener('click', () => {
    const btn = document.getElementById('btn-sync');
    btn.disabled = true;
    btn.textContent = '⏳ Đang quét & đồng bộ…';
    chrome.runtime.sendMessage({ action: 'sync_now' }, (res) => {
        chrome.storage.local.get(['status', 'username', 'lastSync', 'error', 'detectedDetails'], (st) => {
            renderState(st);
            btn.disabled = false;
            btn.textContent = '🔄 Đồng bộ ngay';
        });
    });
});

document.getElementById('btn-open-vjudge').addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://vjudge.net' });
});

document.getElementById('btn-open-connect').addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://omnijudge.id.vn/user/vjudge/connect/' });
});
