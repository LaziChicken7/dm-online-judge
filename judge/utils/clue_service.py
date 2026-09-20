import http.cookiejar
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import urllib.error

logger = logging.getLogger('judge.clue_service')

CLUE_BASE_URL = "https://oj.clue.edu.vn"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'vi,en-US;q=0.9,en;q=0.8',
}

# Mapping from local DMOJ Language keys to ClueOJ language IDs
LANG_MAP = {
    'C': 5,
    'C11': 6,
    'CPP03': 1,
    'CPP11': 2,
    'CPP14': 3,
    'CPP17': 4,
    'CPP20': 14,
    'PY3': 9,
    'PYTHON3': 9,
    'JAVA8': 8,
    'JAVA': 8,
    'PAS': 15,
    'PASCAL': 15,
}

def map_lang_to_clue(lang_key: str) -> int:
    if not lang_key:
        return 4  # Default C++17
    key = str(lang_key).upper()
    return LANG_MAP.get(key, 4)


def login_clue(username: str, password: str) -> dict:
    """
    Log in to ClueOJ with credentials, returning session cookies.
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    login_url = f"{CLUE_BASE_URL}/accounts/login/"
    req = urllib.request.Request(login_url, headers=HEADERS)
    try:
        with opener.open(req, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        m = re.search(r'name=[\'"]csrfmiddlewaretoken[\'"]\s+value=[\'"]([^\'"]+)[\'"]', html)
        if not m:
            return {"success": False, "error": "Không tìm thấy CSRF token trên ClueOJ."}
        csrf = m.group(1)

        post_data = urllib.parse.urlencode({
            'csrfmiddlewaretoken': csrf,
            'username': username.strip(),
            'password': password.strip(),
            'next': '/',
        }).encode('utf-8')

        post_headers = dict(HEADERS)
        post_headers['Referer'] = login_url
        post_headers['Origin'] = CLUE_BASE_URL

        req2 = urllib.request.Request(login_url, data=post_data, headers=post_headers)
        with opener.open(req2, timeout=12) as resp2:
            final_url = resp2.geturl()
            cookies = {c.name: c.value for c in cj}

            if 'sessionid' not in cookies:
                # Check for error in html
                body = resp2.read().decode('utf-8', errors='ignore')
                if 'không chính xác' in body.lower() or 'invalid' in body.lower():
                    return {"success": False, "error": "Tên đăng nhập hoặc mật khẩu ClueOJ không chính xác."}
                return {"success": False, "error": "Đăng nhập ClueOJ thất bại, không nhận được phiên làm việc."}

            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            return {
                "success": True,
                "username": username.strip(),
                "password": password.strip(),
                "cookie": cookie_str,
            }
    except Exception as e:
        logger.exception(f"login_clue error: {e}")
        return {"success": False, "error": f"Lỗi kết nối tới ClueOJ: {e}"}


def check_clue_login(cookie: str) -> dict:
    """
    Check if a given ClueOJ cookie has an active session.
    """
    if not cookie or 'sessionid' not in cookie:
        return {"logged_in": False, "error": "Cookie trống hoặc không có sessionid"}

    headers = dict(HEADERS)
    headers['Cookie'] = cookie
    check_url = f"{CLUE_BASE_URL}/edit/profile/"
    req = urllib.request.Request(check_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.geturl()
            if '/accounts/login/' in final_url:
                return {"logged_in": False, "error": "Phiên đăng nhập đã hết hạn"}
            return {"logged_in": True}
    except Exception as e:
        return {"logged_in": False, "error": str(e)}


def get_profile_clue_opener(profile):
    """
    Return an authenticated opener and cookie string for the profile.
    If cookie is expired and password exists, auto-re-login.
    """
    if not profile or not profile.clue_username:
        # Fallback to test user or configured default if any
        return None, None

    cookie = profile.clue_cookie
    if cookie and check_clue_login(cookie).get('logged_in'):
        cj = http.cookiejar.CookieJar()
        # Parse cookie into jar
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        return opener, cookie

    # Try re-login if password is saved
    if profile.clue_username and profile.clue_password:
        res = login_clue(profile.clue_username, profile.clue_password)
        if res.get('success') and res.get('cookie'):
            profile.clue_cookie = res['cookie']
            profile.save(update_fields=['clue_cookie'])
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            return opener, res['cookie']

    return None, None


def submit_clue_solution(profile, prob_code: str, lang_id: int, source: str) -> dict:
    """
    Submit source code to ClueOJ problem, returning remote submission ID.
    """
    opener, cookie = get_profile_clue_opener(profile)
    if not opener or not cookie:
        return {"success": False, "error": "Chưa kết nối tài khoản ClueOJ hoặc phiên đăng nhập đã hết hạn."}

    sub_url = f"{CLUE_BASE_URL}/problem/{prob_code}/submit"
    req_headers = dict(HEADERS)
    req_headers['Cookie'] = cookie

    try:
        # 1. GET submit page to get CSRF
        req_get = urllib.request.Request(sub_url, headers=req_headers)
        with opener.open(req_get, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        m = re.search(r'name=[\'"]csrfmiddlewaretoken[\'"]\s+value=[\'"]([^\'"]+)[\'"]', html)
        if not m:
            return {"success": False, "error": "Không tìm thấy CSRF trên trang nộp bài ClueOJ."}
        sub_csrf = m.group(1)

        # 2. POST submit
        post_data = urllib.parse.urlencode({
            'csrfmiddlewaretoken': sub_csrf,
            'language': str(lang_id),
            'source': source,
        }).encode('utf-8')

        post_headers = dict(req_headers)
        post_headers['Referer'] = sub_url
        post_headers['Origin'] = CLUE_BASE_URL

        req_post = urllib.request.Request(sub_url, data=post_data, headers=post_headers)
        with opener.open(req_post, timeout=15) as resp2:
            final_url = resp2.geturl()
            sub_id_m = re.search(r'/submission/(\d+)', final_url)
            if sub_id_m:
                return {"success": True, "submission_id": int(sub_id_m.group(1))}

            body = resp2.read().decode('utf-8', errors='ignore')
            if 'errorlist' in body:
                err = re.search(r'<ul class="errorlist">([\s\S]*?)</ul>', body)
                err_text = re.sub(r'<[^>]+>', ' ', err.group(1)).strip() if err else "Lỗi nộp bài"
                return {"success": False, "error": f"ClueOJ báo lỗi: {err_text}"}

            return {"success": False, "error": f"Không nhận được submission ID từ ClueOJ (URL: {final_url})."}
    except Exception as e:
        logger.exception(f"submit_clue_solution error for {prob_code}: {e}")
        return {"success": False, "error": str(e)}


def poll_clue_submission(profile, sub_id: int) -> dict:
    """
    Fetch submission status, verdict, points, time, memory, testcases from ClueOJ.
    """
    opener, cookie = get_profile_clue_opener(profile)
    headers = dict(HEADERS)
    if cookie:
        headers['Cookie'] = cookie

    url = f"{CLUE_BASE_URL}/submission/{sub_id}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        # Check if grading is complete: on DMOJ/ClueOJ, "Điểm cuối cùng:" is only rendered when finished
        has_final_score = any(w in html.lower() for w in ['điểm cuối cùng', 'final score', 'lỗi dịch', 'lỗi biên dịch', 'compile error'])
        is_done = has_final_score

        # Check testcases
        cases = []
        case_rows = re.findall(r'<tr[^>]*class="case-row[^"]*"[^>]*>([\s\S]*?)</tr>', html)
        for row in case_rows:
            case_num = len(cases) + 1

            status = "WA"
            for s in ["AC", "WA", "TLE", "MLE", "RTE", "CE", "IR"]:
                if f"case-{s}" in row:
                    status = s
                    break

            pts_m = re.search(r'\((\d+)/(\d+)\)', row)
            c_pts = float(pts_m.group(1)) if pts_m else (1.0 if status == "AC" else 0.0)
            c_tot = float(pts_m.group(2)) if pts_m else 1.0

            time_m = re.search(r'title="([0-9.]+)\s*s"', row)
            if not time_m:
                time_m = re.search(r'([0-9.,]+)\s*s', row)
            c_time = float(time_m.group(1).replace(',', '.')) if time_m else 0.0

            mem_m = re.search(r'([0-9.,]+)\s*(?:&nbsp;|\s| )*(MB|KB|M|K|B)', row, re.I)
            if mem_m:
                mem_val = float(mem_m.group(1).replace(',', '.'))
                mem_unit = mem_m.group(2).lower()
                c_mem_kb = int(mem_val * 1024) if 'm' in mem_unit else int(mem_val)
            else:
                c_mem_kb = 0

            cases.append({
                "case": case_num,
                "status": status,
                "points": c_pts,
                "total": c_tot,
                "time": c_time,
                "memory": c_mem_kb,
            })

        # Overall verdict
        if not cases:
            if not is_done:
                return {"done": False, "verdict": "QU", "points": 0, "cases": []}
            overall = "CE" if any(w in html.lower() for w in ['lỗi dịch', 'biên dịch', 'compile error']) else "WA"
        elif all(c["status"] == "AC" for c in cases):
            overall = "AC"
        else:
            bad = next((c["status"] for c in cases if c["status"] != "AC"), "WA")
            overall = bad

        # Calculate final points
        total_pts = 0.0
        max_pts = 100.0
        m_score = re.search(r'Điểm cuối cùng:[\s\S]*?(\d+)/(\d+)', html)
        if m_score:
            total_pts = float(m_score.group(1))
            max_pts = float(m_score.group(2))
        else:
            pts = re.findall(r'\(\s*([0-9,.]+)\s*/\s*([0-9,.]+)\s*(điểm|pts|points)?\s*\)', html)
            if pts:
                for p, m, _ in pts:
                    try:
                        total_pts += float(p.replace(',', '.'))
                        max_pts += float(m.replace(',', '.'))
                    except Exception:
                        pass
            else:
                total_pts = sum(c["points"] for c in cases)
                max_pts = sum(c["total"] for c in cases) or 100.0

        max_time = max([c["time"] for c in cases], default=0.0)
        max_mem = max([c["memory"] for c in cases], default=0)

        ce_feedback = ""
        m_ce = re.search(r'<pre class="case-output">([\s\S]*?)</pre>', html)
        if m_ce:
            ce_feedback = re.sub(r'<[^>]+>', ' ', m_ce.group(1)).strip()

        return {
            "done": is_done,
            "verdict": overall,
            "points": total_pts,
            "max_points": max_pts,
            "time": max_time,
            "memory": max_mem,
            "feedback": ce_feedback,
            "cases_count": len(cases),
            "cases": cases,
        }

    except Exception as e:
        logger.warning(f"poll_clue_submission error for {sub_id}: {e}")
        return {"done": False, "error": str(e)}
