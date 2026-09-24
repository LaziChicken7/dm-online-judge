import json
import logging
import re
import urllib.parse
import urllib.request
import urllib.error

logger = logging.getLogger('judge.vjudge_service')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
}



def normalize_vjudge_cookie(cookie_str: str) -> str:
    """
    Normalize user-entered VJudge cookie to valid HTTP Cookie header format.
    Handles raw session token, full cookie headers, or JSESSIONID=token.
    Encodes illegal header characters like | to %7C to avoid Cloudflare 403 WAF blocks.
    """
    if not cookie_str:
        return ""
    cookie_str = cookie_str.strip().strip('"').strip("'")
    cookie_str = cookie_str.replace('|', '%7C')
    if '=' not in cookie_str and len(cookie_str) >= 16:
        return f"JSESSIONID={cookie_str}"
    if 'JSESSIONID=' in cookie_str or 'JSESSlONID=' in cookie_str:
        return "; ".join(part.strip() for part in cookie_str.split(';') if part.strip())
    return cookie_str

def check_vjudge_login(cookie: str) -> dict:
    """
    Check if the given cookie has an active login session on VJudge.
    Calls https://vjudge.net/user/checkLogInStatus.
    """
    if not cookie:
        return {"logged_in": False, "error": "Cookie is empty"}

    url = "https://vjudge.net/user/checkLogInStatus"
    headers = dict(HEADERS)
    headers['Cookie'] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            text = resp.read().decode('utf-8', errors='ignore').strip()
            # VJudge returns true or username string or json
            if text in ('true', '1') or (text.startswith('{') and 'username' in text):
                return {"logged_in": True, "raw": text}
            return {"logged_in": False, "raw": text}
    except Exception as e:
        logger.warning(f"check_vjudge_login error: {e}")
        return {"logged_in": False, "error": str(e)}


def get_vjudge_remote_accounts(cookie: str, oj: str = "CodeForces") -> list:
    """
    Get user's connected remote accounts on VJudge for a given OJ.
    Calls https://vjudge.net/user/remoteAccounts/list?oj=CodeForces.
    Returns list of dicts with:
      id, accountId, remoteUsername, username, oj, runtimeStatus, healthStatus, isIncomplete, isReady
    """
    if not cookie:
        return []

    import hashlib
    from django.core.cache import cache
    cache_key = f"vjudge_remote_accs_{hashlib.md5(f'{cookie}:{oj}'.encode()).hexdigest()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://vjudge.net/user/remoteAccounts/list?oj={urllib.parse.quote(oj)}"
    headers = dict(HEADERS)
    headers['Cookie'] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            bindings = []
            if isinstance(data, list):
                bindings = data
            elif isinstance(data, dict):
                groups = data.get("groups", {})
                for group_name, group_data in groups.items():
                    if group_name.lower() == (oj or "").lower():
                        bindings = group_data.get("bindings", [])
                        break
                if not bindings and "bindings" in data:
                    bindings = data["bindings"]

            result = []
            for b in bindings:
                acc_id = b.get("id")
                username = b.get("accountId") or b.get("remoteUsername") or b.get("username") or ""
                runtime_status = b.get("runtimeStatus") or ""
                health_status = b.get("healthStatus") or ""
                is_incomplete = bool(b.get("isIncomplete", False)) or (runtime_status == "MISSING_BINDING") or (health_status == "INCOMPLETE")
                result.append({
                    "id": acc_id,
                    "accountId": username,
                    "remoteUsername": username,
                    "username": username,
                    "oj": b.get("oj", oj),
                    "runtimeStatus": runtime_status,
                    "healthStatus": health_status,
                    "isIncomplete": is_incomplete,
                    "isReady": (runtime_status == "READY" and not is_incomplete),
                })
            cache.set(cache_key, result, 60)
            return result
    except Exception as e:
        logger.warning(f"get_vjudge_remote_accounts error: {e}")
        return []


def submit_vjudge_solution(
    oj: str,
    prob_num: str,
    language: str,
    source: str,
    cookie: str,
    method: int = 0,
    binding_id: int = None,
    open_code: int = 1
) -> dict:
    """
    Submit code to VJudge:
    POST https://vjudge.net/problem/submit/{oj}-{probNum}
    data: method (0/1/2), language, open, source, bindingId (if method==1)
    Returns: {"success": True, "runId": ...} or {"success": False, "error": ...}
    """
    url = f"https://vjudge.net/problem/submit/{oj}-{prob_num}"
    post_data = {
        'method': str(method),
        'language': str(language),
        'open': str(open_code),
        'source': source,
    }
    if method == 1 and binding_id:
        post_data['bindingId'] = str(binding_id)

    encoded_data = urllib.parse.urlencode(post_data).encode('utf-8')
    headers = dict(HEADERS)
    headers['Cookie'] = normalize_vjudge_cookie(cookie) or ''
    headers['Referer'] = f"https://vjudge.net/problem/{oj}-{prob_num}"
    headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'

    req = urllib.request.Request(url, data=encoded_data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            run_id = data.get('runId')
            if run_id:
                return {"success": True, "runId": int(run_id)}
            error = data.get('error') or data.get('errorKey') or data.get('i18nKey') or data
            return {"success": False, "error": str(error)}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        logger.error(f"VJudge submit HTTP {e.code}: {body}")
        return {"success": False, "error": f"VJudge HTTP {e.code}: {body}"}
    except Exception as e:
        logger.exception(f"VJudge submit exception: {e}")
        return {"success": False, "error": str(e)}


def poll_vjudge_submission(
    run_id: int,
    username: str = None,
    oj: str = None,
    prob_num: str = None,
    cookie: str = None
) -> dict:
    """
    Poll status of a VJudge submission.
    Uses public https://vjudge.net/status/data?start=0&length=10&OJId=...&probNum=...
    and fallback to https://vjudge.net/solution/data/{runId} if cookie provided.
    Returns:
      {
        "done": bool,
        "status": str ("Accepted", "Wrong Answer", "Queuing && Judging", etc.),
        "runtime": int (ms),
        "memory": int (KB),
        "additional_info": str,
        "raw": dict
      }
    """
    # 1. Try solution/data/{runId} if cookie exists
    effective_cookie = cookie or get_any_vjudge_cookie()
    if effective_cookie:
        sol_url = f"https://vjudge.net/solution/data/{run_id}"
        headers = dict(HEADERS)
        headers['Cookie'] = effective_cookie
        headers['Origin'] = 'https://vjudge.net'
        headers['Referer'] = 'https://vjudge.net/status'
        headers['X-Requested-With'] = 'XMLHttpRequest'
        post_data = urllib.parse.urlencode({'shareCode': ''}).encode('utf-8')
        try:
            req = urllib.request.Request(sol_url, data=post_data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8', errors='ignore'))
                if not data.get('error'):
                    status_text = data.get('status') or ''
                    processing = bool(data.get('processing', False))
                    st_lower = status_text.lower()
                    if 'compilation error' in st_lower or 'compile error' in st_lower:
                        processing = False
                    elif any(w in st_lower for w in ['queuing', 'judging', 'compiling', 'running', 'pending', 'submitted']):
                        processing = True
                    add_info = data.get('additionalInfo')
                    add_info_str = ''
                    if isinstance(add_info, dict):
                        add_info_str = add_info.get('html') or str(add_info)
                    elif add_info:
                        add_info_str = str(add_info)
                    return {
                        "done": not processing,
                        "status": status_text,
                        "runtime": int(data.get('runtime') or 0),
                        "memory": int(data.get('memory') or 0),
                        "additional_info": add_info_str,
                        "remote_run_id": data.get('remoteRunId'),
                        "raw": data,
                    }
        except Exception:
            pass

    # 2. Query public status/data
    params = {'start': 0, 'length': 20}
    if username:
        params['un'] = username
    if oj:
        params['OJId'] = oj
    if prob_num:
        params['probNum'] = prob_num

    query_str = urllib.parse.urlencode(params)
    url = f"https://vjudge.net/status/data?{query_str}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            records = data.get('data', [])
            target = next((r for r in records if r.get('runId') == run_id), None)
            if not target and records:
                target = records[0]

            if target:
                status_text = target.get('status') or ''
                processing = bool(target.get('processing', False))
                st_lower = status_text.lower()
                if 'compilation error' in st_lower or 'compile error' in st_lower:
                    processing = False
                elif any(w in st_lower for w in ['queuing', 'judging', 'compiling', 'running', 'pending', 'submitted']):
                    processing = True

                add_info_str = ''
                remote_run_id = target.get('remoteRunId')
                if not processing and effective_cookie:
                    try:
                        sol_url = f"https://vjudge.net/solution/data/{run_id}"
                        s_headers = dict(HEADERS)
                        s_headers['Cookie'] = effective_cookie
                        s_headers['Origin'] = 'https://vjudge.net'
                        s_headers['Referer'] = 'https://vjudge.net/status'
                        s_headers['X-Requested-With'] = 'XMLHttpRequest'
                        s_post = urllib.parse.urlencode({'shareCode': ''}).encode('utf-8')
                        s_req = urllib.request.Request(sol_url, data=s_post, headers=s_headers)
                        with urllib.request.urlopen(s_req, timeout=8) as s_resp:
                            s_data = json.loads(s_resp.read().decode('utf-8', errors='ignore'))
                            if not s_data.get('error'):
                                add_info = s_data.get('additionalInfo')
                                if isinstance(add_info, dict):
                                    add_info_str = add_info.get('html') or str(add_info)
                                elif add_info:
                                    add_info_str = str(add_info)
                                if s_data.get('remoteRunId'):
                                    remote_run_id = s_data.get('remoteRunId')
                    except Exception:
                        pass

                return {
                    "done": not processing,
                    "status": status_text,
                    "runtime": int(target.get('runtime') or 0),
                    "memory": int(target.get('memory') or 0),
                    "additional_info": add_info_str,
                    "remote_run_id": remote_run_id,
                    "raw": target,
                }
    except Exception as e:
        logger.warning(f"poll_vjudge_submission error: {e}")

    return {
        "done": False,
        "status": "Queuing && Judging",
        "runtime": 0,
        "memory": 0,
        "additional_info": "",
        "raw": {},
    }


def login_vjudge(username: str, password: str) -> dict:
    """
    Automatically log in to VJudge with username and password,
    and extract authenticated session cookies.
    """
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    login_url = "https://vjudge.net/user/login"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://vjudge.net',
        'Referer': 'https://vjudge.net/',
    }

    data = urllib.parse.urlencode({
        'username': username.strip(),
        'password': password.strip(),
    }).encode('utf-8')

    req = urllib.request.Request(login_url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=15) as resp:
            body = resp.read().decode('utf-8', errors='ignore')
            cookie_parts = []
            for c in cj:
                cookie_parts.append(f"{c.name}={c.value}")
            cookie_str = "; ".join(cookie_parts)

            if not cookie_str:
                return {"success": False, "error": "Không nhận được cookie sau khi đăng nhập."}

            return {
                "success": True,
                "cookie": cookie_str,
                "username": username.strip(),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        logger.warning(f"login_vjudge HTTP {e.code}: {body}")
        try:
            err_data = json.loads(body)
            key = err_data.get('i18nKey', '')
            if 'invalid_credentials' in key:
                return {"success": False, "error": "Tên đăng nhập hoặc mật khẩu Virtual Judge không chính xác."}
            elif 'human_verification' in key:
                return {
                    "success": False,
                    "is_turnstile": True,
                    "error": "Virtual Judge (vjudge.net) yêu cầu xác minh bảo mật (Cloudflare Turnstile CAPTCHA). Vui lòng sử dụng phương thức 'Kết nối bằng Session Cookie (JSESSIONID)' bên dưới để liên kết."
                }
        except Exception:
            pass
        return {"success": False, "error": f"Lỗi đăng nhập ({e.code}): {body}"}
    except Exception as e:
        logger.exception(f"login_vjudge exception: {e}")
        return {"success": False, "error": str(e)}


def get_any_vjudge_cookie() -> str:
    """
    Get a working VJudge cookie from any connected user profile in the database.
    Only returns valid-looking cookies (no illegal characters like | or non-ASCII).
    """
    try:
        from judge.models import Profile
        for p in Profile.objects.filter(vjudge_cookie__isnull=False).exclude(vjudge_cookie=''):
            c = (p.vjudge_cookie or '').strip()
            if c and not any(ch in c for ch in ['|', '\n', '\r', '<', '>']) and 'JSESSIONID' in c:
                return c
    except Exception:
        pass
    return ""


def get_vjudge_problem_data(oj: str, prob_num: str, cookie: str = None) -> dict:
    """
    Fetch problem page from VJudge and extract dataJson.
    Contains descBriefs, languages, properties, submitMethods.
    """
    effective_cookie = cookie or get_any_vjudge_cookie()
    url = f"https://vjudge.net/problem/{oj}-{prob_num}"

    def _fetch(c: str = None):
        headers = dict(HEADERS)
        if c:
            headers['Cookie'] = c
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode('utf-8', errors='ignore')

    html = None
    try:
        if effective_cookie:
            try:
                html = _fetch(effective_cookie)
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    logger.warning(f"get_vjudge_problem_data got 403 with cookie, retrying without cookie...")
                    html = _fetch(None)
                else:
                    raise
        else:
            html = _fetch(None)

        if html:
            m = re.search(r'<textarea[^>]*name=[\'\"]dataJson[\'\"][^>]*>(.*?)</textarea>', html, re.DOTALL)
            if m:
                return json.loads(m.group(1))
    except Exception as e:
        logger.warning(f"get_vjudge_problem_data error: {e}")
    return None


def clean_vjudge_math(txt: str) -> str:
    """
    Convert LaTeX formulas from VJudge/Codeforces/CSES format to DMOJ MathJax format:
    0. Codeforces 6-dollar display math $$$$$$...$$$$$$ -> \[...\]
    1. CSES KaTeX math spans:
       <span class="math math-inline">$ n $</span> -> ~n~
       <span class="math math-display">$$ ... $$</span> -> \n\n\[...\]\n\n
    2. Codeforces triple dollars $$$...$$$ -> ~...~
    3. LaTeX display math: $$...$$ -> \[...\]
    4. LaTeX inline math: $...$ (not $$) -> ~...~
    Leaves \\[...\\] and ~...~ intact.
    """
    if not txt:
        return ""

    # 0. Codeforces display math stored by VJudge as 6 dollars: $$$$$$...$$$$$$ -> \[...\]
    #    This MUST come before the $$$...$$$ step or it gets mis-split into empty matches.
    def replace_six_dollar(m):
        inner = m.group(1).strip()
        return f"\n\n\[{inner}\]\n\n"

    txt = re.sub(r'\${6}(.*?)\${6}', replace_six_dollar, txt, flags=re.DOTALL)

    # Also handle 4-dollar display math $$$$...$$$$ (2-dollar CF wrapped)
    def replace_four_dollar(m):
        inner = m.group(1).strip()
        return f"\n\n\[{inner}\]\n\n"

    txt = re.sub(r'\${4}(.*?)\${4}', replace_four_dollar, txt, flags=re.DOTALL)

    # 1. Clean math-display spans (strip any $$ or $ and extra spaces inside)
    def replace_display(m):
        inner = m.group(1).strip()
        inner = re.sub(r'^\$+|\$+$', '', inner).strip()
        return f"\n\n\[{inner}\]\n\n"

    txt = re.sub(r'<span class=[\'"]math math-display[\'"]>(.*?)</span>', replace_display, txt, flags=re.DOTALL)

    # 2. Clean math-inline spans (strip any $ and extra spaces inside)
    def replace_inline(m):
        inner = m.group(1).strip()
        inner = re.sub(r'^\$+|\$+$', '', inner).strip()
        return f"~{inner}~"

    txt = re.sub(r'<span class=[\'"]math math-inline[\'"]>(.*?)</span>', replace_inline, txt, flags=re.DOTALL)

    # 3. Clean any generic math spans
    def replace_any_math(m):
        inner = m.group(1).strip()
        inner = re.sub(r'^\$+|\$+$', '', inner).strip()
        return f"~{inner}~"

    txt = re.sub(r'<span class=[\'"]math[\'"]>(.*?)</span>', replace_any_math, txt, flags=re.DOTALL)

    # 4. Codeforces triple dollars $$$...$$$ -> ~...~  (inline; comes after 6/4 dollar handling)
    txt = re.sub(r'\$\$\$(.*?)\$\$\$', r'~\1~', txt, flags=re.DOTALL)

    # 5. LaTeX display math $$...$$ (standard) -> \[...\]
    def replace_double_dollar(m):
        inner = m.group(1).strip()
        return f"\n\n\[{inner}\]\n\n"

    txt = re.sub(r'(?<!\$)\$\$((?!\$).*?(?<!\$))\$\$(?!\$)', replace_double_dollar, txt, flags=re.DOTALL)

    # 6. LaTeX inline math $...$ -> ~...~ (when not $$)
    def replace_single_dollar(m):
        inner = m.group(1).strip()
        return f"~{inner}~"

    txt = re.sub(r'(?<!\\)(?<!\$)\$(?!\$)([^\$\n]+?)(?<!\\)(?<!\$)\$(?!\$)', replace_single_dollar, txt)

    return txt


def select_best_vjudge_statement(statements: list, user_lang: str = None, requested_key: str = None) -> dict:
    """
    Choose default statement for a VJudge problem:
    1. If a specific key is requested, select it if present.
    2. Match user's preferred language (e.g. 'vi', 'en', 'zh').
       - If multiple statements exist for that language:
         prefer official/main, then GPT/DeepSeek translators, then first.
    3. If user's language is not available, default to English ('en').
       - Prefer official/main English statement, then first English statement.
    4. Fallback to official statement or first available statement.
    """
    if not statements:
        return None

    if requested_key:
        for s in statements:
            if str(s.get("key")) == str(requested_key):
                return s

    def normalize_lang(code):
        if not code:
            return ""
        return code.split("-")[0].split("_")[0].lower().strip()

    norm_user = normalize_lang(user_lang) or "en"

    def find_in_lang(lang_code):
        candidates = [
            s for s in statements
            if normalize_lang(s.get("lang")) == lang_code or s.get("lang", "").lower().startswith(lang_code)
        ]
        if not candidates:
            return None
        for c in candidates:
            if c.get("official") or c.get("type") == "main":
                return c
        for c in candidates:
            if c.get("author") in ("GPT", "DeepSeek"):
                return c
        return candidates[0]

    # 1. Try user's chosen language
    chosen = find_in_lang(norm_user)
    if chosen:
        return chosen

    # 2. Fallback to English ('en') if chosen language was not English
    if norm_user != "en":
        chosen = find_in_lang("en")
        if chosen:
            return chosen

    # 3. Fallback to official or main
    for s in statements:
        if s.get("official") or s.get("type") == "main":
            return s

    return statements[0]


def get_vjudge_statement_content(key: str, cookie: str = None) -> str:
    """
    Fetch problem description HTML by statement key from VJudge.
    Extracts data-json-container and formats clean HTML with DMOJ MathJax (~...~).
    """
    effective_cookie = cookie or get_any_vjudge_cookie()
    url = f"https://vjudge.net/problem/description/{key}"

    def _fetch(c: str = None):
        headers = dict(HEADERS)
        if c:
            headers['Cookie'] = c
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode('utf-8', errors='ignore')

    html = None
    try:
        if effective_cookie:
            try:
                html = _fetch(effective_cookie)
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    logger.warning(f"get_vjudge_statement_content got 403 with cookie for key {key}, retrying without cookie...")
                    html = _fetch(None)
                else:
                    raise
        else:
            html = _fetch(None)

        if html:
            m = re.search(r'<textarea[^>]*data-json-container[^>]*>(.*?)</textarea>', html, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
                sections = data.get('sections', [])
                html_parts = []

                for s in sections:
                    title = s.get('title')
                    val_obj = s.get('value', '')
                    if isinstance(val_obj, dict):
                        val_text = val_obj.get('content', '')
                    else:
                        val_text = str(val_obj)

                    val_text = clean_vjudge_math(val_text)
                    title = clean_vjudge_math(title) if title else ""

                    if title:
                        html_parts.append(f'<div class="vjudge-section-heading">{title}</div>')
                    if val_text:
                        html_parts.append(f'<div class="vjudge-section-body">{val_text}</div>')

                return "\n".join(html_parts)
            return clean_vjudge_math(html)
    except Exception as e:
        logger.warning(f"get_vjudge_statement_content error for key {key}: {e}")
        return f"<div class='alert alert-warning'>Không thể tải nội dung đề bài (Lỗi: {e}).</div>"
    return ""
