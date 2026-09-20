import html as pyhtml
import http.cookiejar
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Dict, Optional, List

logger = logging.getLogger('judge.ltpt_service')

LTPT_BASE_URL = "https://laptrinhphothong.vn"

# Compiler IDs on LapTrinhPhoThong (DMOJ-based)
LTPT_LANGUAGE_MAP = {
    'C': '9',
    'C11': '72',
    'CPP': '69',       # C++17
    'CPP03': '2',
    'CPP11': '13',
    'CPP14': '33',
    'CPP17': '69',
    'CPP20': '76',
    'JAVA': '25',      # Java 8
    'JAVA8': '25',
    'PAS': '10',       # Pascal
    'PASCAL': '10',
    'PY2': '1',        # Python 2
    'PY3': '8',        # Python 3
    'PYTHON2': '1',
    'PYTHON3': '8',
    'TEXT': '51',
}

def clean_ltpt_math(txt: str) -> str:
    if not txt:
        return ""
    # Convert \(...\) to ~...~
    txt = re.sub(r'\\\((.*?)\\\)', r'~\1~', txt, flags=re.DOTALL)
    # Convert \[...\] to $$...$$
    txt = re.sub(r'\\\[(.*?)\\\]', r'$$\1$$', txt, flags=re.DOTALL)
    # Convert $$$...$$$ to ~...~
    txt = re.sub(r'\$\$\$(.*?)\$\$\$', r'~\1~', txt, flags=re.DOTALL)
    # Convert single $...$ to ~...~
    txt = re.sub(r'(?<!\\)(?<!\$)\$(?!\s)([^\$\n]*?\S)(?<!\\)\$(?!\$)', r'~\1~', txt)
    return txt

def format_ltpt_samples(html_text: str) -> str:
    """
    Format standard LTPT/DMOJ Input/Output headers into <table class="vjudge_sample">.
    """
    if not html_text:
        return ""

    # Match <h2>Input</h2>\s*<pre><code>...</code></pre>\s*<h2>Output</h2>\s*<pre><code>...</code></pre>
    pattern = r'<h[2-4][^>]*>\s*(Input|Dữ liệu vào)\s*</h[2-4]>\s*<pre[^>]*><code[^>]*>([\s\S]*?)</code></pre>\s*<h[2-4][^>]*>\s*(Output|Kết quả ra|Kết quả)\s*</h[2-4]>\s*<pre[^>]*><code[^>]*>([\s\S]*?)</code></pre>'

    def replace_sample(m):
        inp = pyhtml.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()
        out = pyhtml.unescape(re.sub(r'<[^>]+>', '', m.group(4))).strip()
        return f"""
        <table class="vjudge_sample">
            <thead>
                <tr>
                    <th>Input</th>
                    <th>Output</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><pre>{pyhtml.escape(inp)}</pre></td>
                    <td><pre>{pyhtml.escape(out)}</pre></td>
                </tr>
            </tbody>
        </table>
        """

    return re.sub(pattern, replace_sample, html_text, flags=re.I)

def login_ltpt(username: str, password: str) -> Dict:
    """
    Log in to LapTrinhPhoThong (laptrinhphothong.vn) with username and password.
    Returns: {"success": bool, "cookie": str, "username": str, "error": str}
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    login_url = f"{LTPT_BASE_URL}/accounts/login/"
    req_get = urllib.request.Request(login_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        resp1 = opener.open(req_get, timeout=15)
        html1 = resp1.read().decode("utf-8", errors="ignore")

        csrf_token = None
        for c in cj:
            if c.name == "csrftoken":
                csrf_token = c.value
        if not csrf_token:
            m_csrf = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\'](.*?)["\']', html1)
            if m_csrf:
                csrf_token = m_csrf.group(1)

        if not csrf_token:
            return {"success": False, "error": "Không thể lấy CSRF token từ trang đăng nhập LTPT"}

        data = urllib.parse.urlencode({
            "username": username.strip(),
            "password": password.strip(),
            "csrfmiddlewaretoken": csrf_token,
        }).encode("utf-8")

        req_post = urllib.request.Request(login_url, data=data, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": login_url,
            "Content-Type": "application/x-www-form-urlencoded",
        })

        resp2 = opener.open(req_post, timeout=15)
        body2 = resp2.read().decode("utf-8", errors="ignore")

        cookie_str = "; ".join([f"{c.name}={c.value}" for c in cj])

        has_session = any(c.name == "sessionid" for c in cj)
        is_logged_in = has_session and ("/accounts/logout/" in body2 or username.lower() in body2.lower())

        if is_logged_in:
            return {
                "success": True,
                "cookie": cookie_str,
                "username": username.strip(),
            }

        # Check for error
        m_err = re.search(r'<div class="errorlist"[^>]*>([\s\S]*?)</div>', body2)
        if not m_err:
            m_err = re.search(r'<ul class="errorlist"[^>]*>([\s\S]*?)</ul>', body2)
        err_msg = re.sub(r'<[^>]+>', '', m_err.group(1)).strip() if m_err else "Tên đăng nhập hoặc mật khẩu không chính xác."
        return {"success": False, "error": err_msg}

    except Exception as e:
        logger.exception(f"login_ltpt exception: {e}")
        return {"success": False, "error": str(e)}

def check_ltpt_login(cookie: str) -> Dict:
    """
    Check if the cookie has an active login session on LapTrinhPhoThong.
    """
    if not cookie:
        return {"logged_in": False, "error": "Cookie is empty"}

    url = f"{LTPT_BASE_URL}/"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
    })

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if "/accounts/logout/" in body:
                m_user = re.search(r'/user/([a-zA-Z0-9_-]+)/', body)
                username = m_user.group(1) if m_user else ""
                return {"logged_in": True, "username": username}
            return {"logged_in": False, "error": "Not logged in"}
    except Exception as e:
        logger.warning(f"check_ltpt_login error: {e}")
        return {"logged_in": False, "error": str(e)}

def get_ltpt_problem_data(problem_code_or_url: str) -> Optional[Dict]:
    """
    Fetch and parse a problem from LapTrinhPhoThong by code or URL.
    """
    raw = str(problem_code_or_url).strip()
    m_code = re.search(r'/problem/([a-zA-Z0-9_-]+)', raw)
    if m_code:
        problem_code = m_code.group(1)
    else:
        m_simple = re.match(r'^([a-zA-Z0-9_-]+)$', raw)
        if m_simple:
            problem_code = m_simple.group(1)
        else:
            return None

    url = f"{LTPT_BASE_URL}/problem/{problem_code}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        if "Bài tập không tồn tại" in html or "404" in html:
            return None

        # 1. Problem Title
        m_t = re.search(r'<span class="line-clamp-1 dark:text-white">\s*(.*?)\s*</span>', html)
        if not m_t:
            m_t = re.search(r'<title>(.*?)\s*-\s*Lập trình phổ thông</title>', html)
        problem_title = pyhtml.unescape(m_t.group(1).strip()) if m_t else problem_code

        # 2. Time limit & Memory limit & Points
        time_limit = 1.0
        m_time = re.search(r'Time limit:[\s\S]*?(\d+(\.\d+)?)s', html)
        if m_time:
            try:
                time_limit = float(m_time.group(1))
            except Exception:
                pass

        memory_limit = 262144 # 256MB default
        m_mem = re.search(r'Memory limit:[\s\S]*?(\d+)\s*(Mb|MB|M|K|KB)', html, re.I)
        if m_mem:
            try:
                val = int(m_mem.group(1))
                unit = m_mem.group(2).lower()
                if 'm' in unit:
                    memory_limit = val * 1024
                else:
                    memory_limit = val
            except Exception:
                pass

        points = 100.0
        m_pts = re.search(r'Point:[\s\S]*?</div>\s*<div[^>]*>\s*(\d+(\.\d+)?)', html)
        if m_pts:
            try:
                points = float(m_pts.group(1))
            except Exception:
                pass

        # 3. Description HTML
        m_desc = re.search(r'<div class="p-4 transition-colors bg-white dark:bg-dark-content format-td:font-roboto custom-typography rounded-xl">([\s\S]*?)</div>\s*</div>\s*</div>', html)
        if m_desc:
            raw_desc = m_desc.group(1).strip()
        else:
            # Fallback to article or main
            m_main = re.search(r'<div id="content-body"[\s\S]*?>([\s\S]*?)</div>\s*</div>\s*</div>', html)
            raw_desc = m_main.group(1).strip() if m_main else ""

        # Make relative URLs absolute
        raw_desc = re.sub(r'href="/(.*?)"', rf'href="{LTPT_BASE_URL}/\1"', raw_desc)
        raw_desc = re.sub(r'src="/(.*?)"', rf'src="{LTPT_BASE_URL}/\1"', raw_desc)

        # Format samples into standard vjudge_sample table
        clean_html = format_ltpt_samples(raw_desc)
        clean_html = clean_ltpt_math(clean_html)

        return {
            "problem_code": problem_code,
            "problem_title": problem_title,
            "time_limit": time_limit,
            "memory_limit": memory_limit,
            "points": points,
            "html": clean_html,
            "original_url": url,
        }

    except Exception as e:
        logger.warning(f"get_ltpt_problem_data error: {e}")
        return None

def submit_ltpt_solution(
    cookie: str,
    problem_code: str,
    code: str,
    language_key: str
) -> Dict:
    """
    Submit a solution to LapTrinhPhoThong using the user's session cookie.
    """
    if not cookie:
        return {"success": False, "error": "Chưa có cookie phiên đăng nhập LapTrinhPhoThong"}

    cj = http.cookiejar.CookieJar()
    for item in cookie.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            ck = http.cookiejar.Cookie(
                version=0, name=k, value=v, port=None, port_specified=False,
                domain="laptrinhphothong.vn", domain_specified=True, domain_initial_dot=False,
                path="/", path_specified=True, secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False
            )
            cj.set_cookie(ck)

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    sub_url = f"{LTPT_BASE_URL}/problem/{problem_code}/submit"
    req_get = urllib.request.Request(sub_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        resp_get = opener.open(req_get, timeout=15)
        html_get = resp_get.read().decode("utf-8", errors="ignore")

        m_csrf = re.search(r'name=["\']csrfmiddlewaretoken["\'] value=["\'](.*?)["\']', html_get)
        if not m_csrf:
            return {"success": False, "error": "Không thể lấy CSRF token nộp bài (phiên đăng nhập có thể đã hết hạn)"}
        csrf_token = m_csrf.group(1)

        # Map language
        lang_id = LTPT_LANGUAGE_MAP.get(language_key.upper(), '69') # Default C++17

        data = urllib.parse.urlencode({
            "csrfmiddlewaretoken": csrf_token,
            "language": lang_id,
            "source": code,
            "judge": "",
        }).encode("utf-8")

        req_post = urllib.request.Request(sub_url, data=data, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": sub_url,
            "Content-Type": "application/x-www-form-urlencoded",
        })

        resp_post = opener.open(req_post, timeout=20)
        final_url = resp_post.geturl()

        # Extract submission ID from final URL: /submission/(\d+)
        m_sub = re.search(r'/submission/(\d+)', final_url)
        if m_sub:
            sub_id = int(m_sub.group(1))
            return {"success": True, "submission_id": sub_id}

        post_body = resp_post.read().decode("utf-8", errors="ignore")
        m_sub_body = re.search(r'/submission/(\d+)', post_body)
        if m_sub_body:
            sub_id = int(m_sub_body.group(1))
            return {"success": True, "submission_id": sub_id}

        return {"success": False, "error": "Nộp bài thành công nhưng không tìm thấy ID bài nộp trên LTPT"}

    except Exception as e:
        logger.exception(f"submit_ltpt_solution error: {e}")
        return {"success": False, "error": str(e)}

def poll_ltpt_submission(cookie: str, submission_id: int) -> Dict:
    """
    Poll LapTrinhPhoThong submission status and test case details.
    """
    url = f"{LTPT_BASE_URL}/submission/{submission_id}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
    })

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        html_lower = html.lower()

        # Check if still judging
        if any(w in html_lower for w in ["chờ chấm", "đang chấm", "đang biên dịch", "chưa có kết quả", "grading"]):
            return {
                "is_final": False,
                "status": "QU",
                "verdict_text": "Đang chấm trên LTPT...",
            }

        # Check overall score
        # Điểm cuối cùng: 4/4 (100.0/100 points)
        m_score = re.search(r'Điểm cuối cùng:[\s\S]*?(\d+)/(\d+)\s*\(([\d\.]+)/([\d\.]+)', html)
        if m_score:
            pts_earned = float(m_score.group(3))
            pts_total = float(m_score.group(4))
        else:
            pts_earned = 0.0
            pts_total = 100.0

        # Maximum runtime
        m_rt = re.search(r'Maximum runtime on single test case:[\s\S]*?title="([\d\.]+)s"', html)
        if not m_rt:
            m_rt = re.search(r'Maximum runtime on single test case:[\s\S]*?>([\d\.,]+)s<', html)
        time_seconds = float(m_rt.group(1).replace(',', '.')) if m_rt else 0.0

        # Memory
        m_mem = re.search(r'Tài nguyên:[\s\S]*?([\d\.,]+)\s*MB', html)
        memory_kb = int(float(m_mem.group(1).replace(',', '.')) * 1024) if m_mem else 0

        # Parse individual test cases
        # <div class="w-20"><b>#1</b></div> ... <span title="..." class="font-semibold case-AC">Chấp nhận (AC)</span> ... [<span title="0.069795856s">0,070s,</span>10.32 MB] (1/1)
        cases = []
        raw_cases = re.findall(
            r'<b>#(\d+)</b>[\s\S]*?<span[^>]*class="[^"]*case-([A-Z]+)"[^>]*>(.*?)</span>[\s\S]*?\[<span[^>]*title="([\d\.]+)s"[\s\S]*?([\d\.,]+)\s*MB\][\s\S]*?\((\d+)/(\d+)\)',
            html
        )

        overall_verdict = "AC" if pts_earned >= pts_total and pts_total > 0 else "WA"

        for c_num, c_st, c_name, c_t, c_m, c_pt, c_tot in raw_cases:
            case_idx = int(c_num)
            case_verdict = c_st.upper() # AC, WA, TLE, MLE, RTE, etc.
            if case_verdict != "AC" and overall_verdict == "AC":
                overall_verdict = case_verdict
            cases.append({
                "case": case_idx,
                "status": case_verdict,
                "time": float(c_t),
                "memory": int(float(c_m.replace(',', '.')) * 1024),
                "points": float(c_pt),
                "total": float(c_tot),
            })

        compile_error_text = ""
        if "Lỗi dịch" in html or "Lỗi biên dịch" in html or "case-CE" in html:
            overall_verdict = "CE"
            m_ce = re.search(r'Lỗi dịch</div>\s*<pre[^>]*>([\s\S]*?)</pre>', html)
            if m_ce:
                compile_error_text = re.sub(r'<[^>]+>', '', m_ce.group(1)).strip()

        # If no regex matched cases, check verdict icons
        if not cases and overall_verdict != "CE":
            if "fa-times" in html or "case-WA" in html:
                overall_verdict = "WA"
            elif "case-TLE" in html:
                overall_verdict = "TLE"
            elif "case-MLE" in html:
                overall_verdict = "MLE"
            else:
                overall_verdict = "WA" 

        return {
            "is_final": True,
            "status": "D",
            "verdict": overall_verdict,
            "points": pts_earned,
            "time_seconds": time_seconds,
            "memory_kb": memory_kb,
            "cases": cases,
            "error": compile_error_text,
        }

    except Exception as e:
        logger.warning(f"poll_ltpt_submission error: {e}")
        return {
            "is_final": False,
            "status": "QU",
            "verdict_text": f"Lỗi kết nối tới LTPT: {e}",
        }
