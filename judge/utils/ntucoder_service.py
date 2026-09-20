import http.cookiejar
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger('judge.ntucoder_service')

NTUCODER_BASE_URL = "https://thptchuyen.ntucoder.net"

# Compiler IDs on NTUCoder
COMPILER_MAP = {
    'CPP': 5,
    'CPP03': 1,
    'CPP11': 5,
    'CPP14': 5,
    'CPP17': 5,
    'CPP20': 5,
    'C': 1,
    'PY3': 8,
    'PY2': 7,
    'PYTHON3': 8,
    'PYTHON2': 7,
    'PAS': 4,
    'PASCAL': 4,
    'JAVA': 6,
    'JAVA8': 6,
}

import html as pyhtml

def format_ntucoder_samples(html_text: str) -> str:
    if not html_text or "sample-type" not in html_text:
        return html_text

    def replace_sample_list(match):
        list_html = match.group(0)
        li_items = re.findall(r'<li[^>]*>([\s\S]*?)</li>', list_html)
        if not li_items:
            li_items = [list_html]

        tables_html = []
        for idx, li in enumerate(li_items):
            pairs = re.findall(r'<div class="sample-type">([\s\S]*?)</div>\s*<div class="sample-value">([\s\S]*?)</div>', li)
            if not pairs:
                continue

            input_val = ""
            output_val = ""
            for stype, sval in pairs:
                stype_clean = re.sub(r'<[^>]+>', '', stype).strip().lower()
                sval_clean = sval.strip()
                sval_text = pyhtml.unescape(re.sub(r'<[^>]+>', '', sval_clean)).strip()
                if "in" in stype_clean:
                    input_val = sval_text
                elif "out" in stype_clean:
                    output_val = sval_text

            prefix = f'<div style="font-weight: 700; color: #38bdf8; font-size: 14.5px; margin: 12px 0 6px 0;">Ví dụ {idx + 1}</div>' if len(li_items) > 1 else ''

            tables_html.append(f"""
            {prefix}
            <table class="vjudge_sample">
                <thead>
                    <tr>
                        <th>Input</th>
                        <th>Output</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><pre>{pyhtml.escape(input_val)}</pre></td>
                        <td><pre>{pyhtml.escape(output_val)}</pre></td>
                    </tr>
                </tbody>
            </table>
            """)

        return "\n".join(tables_html)

    return re.sub(r'<ul[^>]*>\s*<li[^>]*>\s*<div class="sample-type">[\s\S]*?</ul>', replace_sample_list, html_text)

def clean_ntucoder_math(txt: str) -> str:
    if not txt:
        return ""
    # Convert $$$...$$$ to ~...~
    txt = re.sub(r'\$\$\$(.*?)\$\$\$', r'~\1~', txt, flags=re.DOTALL)
    # Convert single $...$ to ~...~
    txt = re.sub(r'(?<!\\)(?<!\$)\$(?!\s)([^\$\n]*?\S)(?<!\\)\$(?!\$)', r'~\1~', txt)
    return txt

def login_ntucoder(email: str, password: str) -> Dict:
    """
    Log in to NTUCoder (thptchuyen.ntucoder.net) with email and password.
    Returns: {"success": bool, "cookie": str, "username": str, "error": str}
    """
    login_url = f"{NTUCODER_BASE_URL}/Account/Login"
    data = urllib.parse.urlencode({
        "Email": email.strip(),
        "MatKhau": password.strip(),
        "RememberMe": "true",
    }).encode("utf-8")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": login_url,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(login_url, data=data, headers=headers)

    try:
        resp = opener.open(req, timeout=15)
        body = resp.read().decode("utf-8", errors="ignore")

        cookies = [f"{c.name}={c.value}" for c in cj]
        cookie_str = "; ".join(cookies)

        m_user = re.search(r'/Coder/Details/([a-zA-Z0-9_-]+)', body)
        username = m_user.group(1).strip() if m_user else ""
        if not username and "/Account/LogOut" in body:
            username = email.split('@')[0]

        if ("XXXAuth" in cookie_str or "ASP.NET_SessionId" in cookie_str) and ("/Account/LogOut" in body or username):
            return {
                "success": True,
                "cookie": cookie_str,
                "username": username or email.split('@')[0],
            }

        m_err = re.search(r'<div class="validation-summary-errors">[\s\S]*?<ul>[\s\S]*?<li>(.*?)</li>', body)
        err_msg = m_err.group(1).strip() if m_err else "Tên đăng nhập hoặc mật khẩu NTUCoder không chính xác."
        return {"success": False, "error": err_msg}
    except Exception as e:
        logger.exception(f"login_ntucoder exception: {e}")
        return {"success": False, "error": str(e)}

def check_ntucoder_login(cookie: str) -> Dict:
    """
    Check if the cookie has an active session on NTUCoder.
    """
    if not cookie:
        return {"logged_in": False, "error": "Cookie is empty"}

    url = f"{NTUCODER_BASE_URL}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if "/Account/LogOut" in body:
                m_user = re.search(r'/Coder/Details/([a-zA-Z0-9_-]+)', body)
                username = m_user.group(1).strip() if m_user else ""
                return {"logged_in": True, "username": username}
            return {"logged_in": False, "error": "Not logged in"}
    except Exception as e:
        logger.warning(f"check_ntucoder_login error: {e}")
        return {"logged_in": False, "error": str(e)}

def get_ntucoder_problem_data(problem_id_or_url: str) -> Optional[Dict]:
    """
    Fetch and parse a problem from NTUCoder by ID (e.g. 11042) or URL.
    """
    raw = str(problem_id_or_url).strip()
    m_id = re.search(r'/Problem/Details/(\d+)', raw)
    if m_id:
        problem_id = m_id.group(1)
    else:
        m_dig = re.match(r'^(\d+)$', raw)
        if m_dig:
            problem_id = m_dig.group(1)
        else:
            return None

    url = f"{NTUCODER_BASE_URL}/Problem/Details/{problem_id}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        if "Bài tập không tìm thấy" in html:
            return None

        # 1. Problem Name & Code
        m_name = re.search(r'<div class="problem-name">\s*([\s\S]*?)\s*</div>', html)
        full_name = re.sub(r'\s+', ' ', m_name.group(1)).strip() if m_name else f"NTUCoder - {problem_id}"

        parts = full_name.split('-', 1)
        if len(parts) == 2:
            problem_code = parts[0].strip()
            problem_title = parts[1].strip()
        else:
            problem_code = f"ntu_{problem_id}"
            problem_title = full_name

        # 2. Time limit & Memory limit
        time_limit = 1.0
        m_t = re.search(r'Giới hạn thời gian:\s*(\d+(\.\d+)?)\s*giây', html)
        if m_t:
            try:
                time_limit = float(m_t.group(1))
            except Exception:
                pass

        memory_limit = 262144 # 256MB default
        m_m = re.search(r'Giới hạn bộ nhớ:\s*(\d+)\s*(megabyte|MB)', html, re.I)
        if m_m:
            try:
                memory_limit = int(m_m.group(1)) * 1024
            except Exception:
                pass

        # 3. Content extraction
        m_c = re.search(r'<div class="problem-content">([\s\S]*?)</div>\s*<div class="problem-sample">', html)
        content_html = m_c.group(1).strip() if m_c else ""

        m_s = re.search(r'<div class="problem-sample">([\s\S]*?)</div>\s*<div class="problem-explain">', html)
        sample_html = m_s.group(1).strip() if m_s else ""

        m_e = re.search(r'<div class="problem-explain">([\s\S]*?)</div>\s*</div>', html)
        explain_html = m_e.group(1).strip() if m_e else ""

        combined = f"{content_html}\n{sample_html}\n{explain_html}".strip()
        if not combined:
            m_box = re.search(r'<div class="tm-box2">([\s\S]*?)</div>\s*<script', html)
            combined = m_box.group(1).strip() if m_box else ""

        # Make image and link URLs absolute
        combined = re.sub(r'href="/(.*?)"', rf'href="{NTUCODER_BASE_URL}/\1"', combined)
        combined = re.sub(r'src="/(.*?)"', rf'src="{NTUCODER_BASE_URL}/\1"', combined)

        clean_html = format_ntucoder_samples(clean_ntucoder_math(combined))

        return {
            "problem_id": problem_id,
            "problem_code": problem_code,
            "problem_title": problem_title,
            "full_name": full_name,
            "time_limit": time_limit,
            "memory_limit": memory_limit,
            "html": clean_html,
            "original_url": url,
        }
    except Exception as e:
        logger.warning(f"get_ntucoder_problem_data error: {e}")
        return None

def submit_ntucoder_solution(
    cookie: str,
    problem_id: str,
    problem_code: str,
    code: str,
    language_key: str
) -> Dict:
    """
    Submit a solution to NTUCoder using CookieJar to handle anti-forgery cookies.
    """
    if not cookie:
        return {"success": False, "error": "Chưa có cookie phiên đăng nhập NTUCoder"}

    # 1. Map language to CompilerID
    compiler_id = COMPILER_MAP.get(language_key.upper(), 5) # Default GNU C++11

    # 2. Setup CookieJar and load existing cookie
    cj = http.cookiejar.CookieJar()
    for item in cookie.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            ck = http.cookiejar.Cookie(
                version=0, name=k, value=v, port=None, port_specified=False,
                domain="thptchuyen.ntucoder.net", domain_specified=True, domain_initial_dot=False,
                path="/", path_specified=True, secure=False, expires=None, discard=True,
                comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False
            )
            cj.set_cookie(ck)

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # 3. GET Submit form using opener to collect __RequestVerificationToken cookie
    sub_page_url = f"{NTUCODER_BASE_URL}/Submission/Submit/?problemid={problem_id}"
    req_get = urllib.request.Request(sub_page_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        resp = opener.open(req_get, timeout=15)
        html = resp.read().decode("utf-8", errors="ignore")

        token_m = re.search(r'name="__RequestVerificationToken" type="hidden" value="(.*?)"', html)
        if not token_m:
            return {"success": False, "error": "Không thể lấy token xác thực nộp bài NTUCoder (phiên đăng nhập có thể đã hết hạn)"}
        token = token_m.group(1)

        code_m = re.search(r'id="ProblemCode" name="ProblemCode" type="hidden" value="(.*?)"', html)
        target_code = code_m.group(1) if code_m else problem_code

        # 4. POST submission multipart form
        submit_url = f"{NTUCODER_BASE_URL}/Submission/Submit"
        boundary = "----WebKitFormBoundaryX7vGz0O8dKp1aM2n"
        lines = []

        def add_field(name, val):
            lines.append(f"--{boundary}")
            lines.append(f'Content-Disposition: form-data; name="{name}"')
            lines.append("")
            lines.append(str(val))

        add_field("__RequestVerificationToken", token)
        add_field("ContestID", "")
        add_field("ProblemCode", target_code)
        add_field("CompilerID", str(compiler_id))
        add_field("SubmitCode", code)

        lines.append(f"--{boundary}")
        lines.append('Content-Disposition: form-data; name="file"; filename=""')
        lines.append("Content-Type: application/octet-stream")
        lines.append("")
        lines.append("")
        lines.append(f"--{boundary}--")
        lines.append("")

        body_bytes = "\r\n".join(lines).encode("utf-8")

        req_post = urllib.request.Request(submit_url, data=body_bytes, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": sub_page_url,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })

        resp_sub = opener.open(req_post, timeout=20)
        sub_body = resp_sub.read().decode("utf-8", errors="ignore")

        # 5. Extract submission ID from response
        m_id = re.search(r'<tr[^>]*id="(\d{5,})"', sub_body)
        if m_id:
            sub_id = int(m_id.group(1))
            return {"success": True, "submission_id": sub_id}

        # Fallback: Query /Submission/Mine/<problem_id> to get the latest submission
        mine_url = f"{NTUCODER_BASE_URL}/Submission/Mine/{problem_id}"
        resp_mine = opener.open(urllib.request.Request(mine_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }), timeout=10)
        mine_html = resp_mine.read().decode("utf-8", errors="ignore")

        m_mine = re.search(r'<tr[^>]*id="(\d{5,})"', mine_html)
        if m_mine:
            sub_id = int(m_mine.group(1))
            return {"success": True, "submission_id": sub_id}

        return {"success": False, "error": "Nộp bài thành công nhưng không tìm thấy ID bài nộp"}

    except Exception as e:
        logger.exception(f"submit_ntucoder_solution error: {e}")
        return {"success": False, "error": str(e)}

def poll_ntucoder_submission(submission_id: int) -> Dict:
    """
    Poll NTUCoder submission status by submission ID.
    Returns parsed verdict, status, time, memory.
    """
    url = f"{NTUCODER_BASE_URL}/Submission/SubmissionBrief/{submission_id}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        tds = re.findall(r'<td[^>]*>([\s\S]*?)</td>', html)
        if len(tds) < 8:
            html_lower = html.lower()
            if "pending" in html_lower or "compil" in html_lower or "running" in html_lower or "judging" in html_lower:
                return {
                    "is_final": False,
                    "status": "QU",
                    "verdict_text": "Đang chấm trên NTUCoder...",
                }

        verdict_raw = re.sub(r'<[^>]+>', '', tds[5]).strip() if len(tds) >= 6 else ""
        time_raw = re.sub(r'<[^>]+>', '', tds[6]).strip() if len(tds) >= 7 else ""
        mem_raw = re.sub(r'<[^>]+>', '', tds[7]).strip() if len(tds) >= 8 else ""

        vt_lower = verdict_raw.lower()
        if not verdict_raw or any(w in vt_lower for w in ["wait", "pending", "compil", "run", "judg", "đang", "chờ"]):
            return {
                "is_final": False,
                "status": "QU",
                "verdict_text": verdict_raw or "Đang chấm trên NTUCoder...",
            }

        t_m = re.search(r'(\d+)\s*ms', time_raw)
        time_ms = float(t_m.group(1)) if t_m else 0.0

        mem_m = re.search(r'(\d+)\s*KB', mem_raw, re.I)
        memory_kb = int(mem_m.group(1)) if mem_m else 0

        if "accepted" in vt_lower:
            verdict = "AC"
            points = 100.0
        elif "wrong answer" in vt_lower or "wa" in vt_lower:
            verdict = "WA"
            points = 0.0
        elif "time limit" in vt_lower or "tle" in vt_lower:
            verdict = "TLE"
            points = 0.0
        elif "memory limit" in vt_lower or "mle" in vt_lower:
            verdict = "MLE"
            points = 0.0
        elif "compile" in vt_lower or "ce" in vt_lower:
            verdict = "CE"
            points = 0.0
        elif "runtime" in vt_lower or "rte" in vt_lower:
            verdict = "RTE"
            points = 0.0
        else:
            verdict = "WA"
            points = 0.0

        tc_m = re.search(r'test\s*(\d+)', verdict_raw, re.I)
        failed_case = int(tc_m.group(1)) if tc_m else None

        return {
            "is_final": True,
            "status": "D",
            "verdict": verdict,
            "verdict_text": verdict_raw,
            "time_seconds": time_ms / 1000.0,
            "memory_kb": memory_kb,
            "points": points,
            "failed_case": failed_case,
        }
    except Exception as e:
        logger.warning(f"poll_ntucoder_submission error: {e}")
        return {
            "is_final": False,
            "status": "QU",
            "verdict_text": f"Lỗi kết nối tới NTUCoder: {e}",
        }
