import logging
import re
import urllib.request
import os
from django.conf import settings
from django.utils import timezone
from judge.models import Language, Problem, ProblemGroup, ProblemType
from judge.utils.vjudge_service import clean_vjudge_math

logger = logging.getLogger('judge.clue_importer')

CLUE_BASE_URL = "https://oj.clue.edu.vn"

def parse_clue_id(text: str) -> str:
    text = (text or '').strip()
    m = re.search(r'clue\.edu\.vn/problem/([a-zA-Z0-9_-]+)', text)
    if m:
        return m.group(1).strip()
    m = re.match(r'^([a-zA-Z0-9_-]+)$', text)
    if m:
        return m.group(1).strip()
    return ""

def clean_problem_code(raw: str, fallback: str = "clue") -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '', raw.lower())
    if not cleaned:
        cleaned = re.sub(r'[^a-zA-Z0-9_]', '', fallback.lower())
    if not cleaned:
        cleaned = "clue"
    return cleaned[:100]

def import_clue_problem(
    clue_input: str,
    code_override: str = None,
    name_override: str = None,
    points_override: str = None,
    time_limit_override: str = None,
    memory_limit_override: str = None,
    is_public: bool = True,
    author_profile = None,
):
    clue_code = parse_clue_id(clue_input)
    if not clue_code:
        raise ValueError(f"Không thể nhận diện mã bài ClueOJ từ '{clue_input}'. Định dạng hợp lệ ví dụ: dhbb26_dx36_10_b hoặc https://oj.clue.edu.vn/problem/dhbb26_dx36_10_b")

    url = f"{CLUE_BASE_URL}/problem/{clue_code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'vi,en-US;q=0.9,en;q=0.8',
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        raise ValueError(f"Không thể kết nối tới bài {clue_code} trên ClueOJ: {e}")

    # 1. Title
    t_m = re.search(r'<div class="problem-title-group">\s*<h2>(.*?)</h2>', html)
    if not t_m:
        t_m = re.search(r'<h2[^>]*>(.*?)</h2>', html)
    clue_title = t_m.group(1).strip() if t_m else clue_code
    clue_title = re.sub(r'<[^>]+>', '', clue_title).strip()

    # 2. Problem Code
    if code_override:
        problem_code = clean_problem_code(code_override)
    else:
        problem_code = clean_problem_code(f"clue_{clue_code}")

    # 3. Problem Name
    problem_name = name_override.strip() if name_override else f"ClueOJ - {clue_code}: {clue_title}"
    if len(problem_name) > 100:
        problem_name = problem_name[:97] + "..."

    # 4. Limits & Points
    time_limit = 1.0
    if time_limit_override:
        try:
            time_limit = float(time_limit_override)
        except (ValueError, TypeError):
            pass
    else:
        time_m = re.search(r'(\d+(\.\d+)?)\s*s', html)
        if time_m:
            try:
                time_limit = float(time_m.group(1))
            except Exception:
                pass
    time_limit = max(0.1, min(60.0, time_limit))

    memory_limit = 262144
    if memory_limit_override:
        try:
            memory_limit = int(memory_limit_override)
        except (ValueError, TypeError):
            pass
    else:
        mem_m = re.search(r'(\d+)\s*(MB|M|K|KB)', html, re.I)
        if mem_m:
            try:
                val = int(mem_m.group(1))
                unit = mem_m.group(2).lower()
                if 'm' in unit:
                    memory_limit = val * 1024
                else:
                    memory_limit = val
            except Exception:
                pass
    memory_limit = max(4096, min(2097152, memory_limit))

    points = 100.0
    if points_override:
        try:
            points = float(points_override)
        except (ValueError, TypeError):
            pass
    else:
        pts_m = re.search(r'(\d+)\s*(điểm|pts|points)', html, re.I)
        if pts_m:
            try:
                points = float(pts_m.group(1))
            except Exception:
                pass

    # 5. Group & Type
    clue_group, _ = ProblemGroup.objects.get_or_create(
        name="ClueOJ",
        defaults={"full_name": "Clue Online Judge"}
    )
    clue_type, _ = ProblemType.objects.get_or_create(
        name="remote",
        defaults={"full_name": "Remote OJ"}
    )

    # 6. Description extraction & cleanup
    # Extract PDF URL if available
    pdf_m = re.search(r'https://oj\.clue\.edu\.vn/pdf/[a-zA-Z0-9_-]+\.pdf', html)
    if not pdf_m:
        pdf_m = re.search(r'/pdf/[a-zA-Z0-9_-]+\.pdf', html)
        pdf_url = f"{CLUE_BASE_URL}{pdf_m.group(0)}" if pdf_m else None
    else:
        pdf_url = pdf_m.group(0)

    desc_m = re.search(r'<div class="content-description screen">([\s\S]*?)</div>\s*<hr>', html)
    if not desc_m:
        desc_m = re.search(r'<div class="content-description screen">([\s\S]*?)</div>', html)

    raw_desc = desc_m.group(1).strip() if desc_m else ""

    # Strip raw object, iframe, embed tags
    raw_desc = re.sub(r'<iframe[\s\S]*?</iframe>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'<object[\s\S]*?</object>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'<embed[\s\S]*?>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'<p>\s*Trong trường hợp đề bài hiển thị không chính xác[\s\S]*?</p>', '', raw_desc, flags=re.I)

    # Rewrite relative URLs to absolute ClueOJ URLs
    raw_desc = re.sub(r'href="/(.*?)"', rf'href="{CLUE_BASE_URL}/\1"', raw_desc)
    raw_desc = re.sub(r'src="/(.*?)"', rf'src="{CLUE_BASE_URL}/\1"', raw_desc)
    raw_desc = re.sub(r'data="/(.*?)"', rf'data="{CLUE_BASE_URL}/\1"', raw_desc)

    # Clean LaTeX math
    # Strip unnecessary outer <div> tags that could unbalance DOM
    if raw_desc.strip().startswith('<div>') and raw_desc.count('<div') > raw_desc.count('</div>'):
        raw_desc = raw_desc.strip()[5:].strip()
    clean_desc = clean_vjudge_math(raw_desc)
    pdf_marker = f"<!-- CLUE_PDF:{pdf_url} -->\n" if pdf_url else ""
    desc = f"{pdf_marker}{clean_desc}".strip()

    defaults = {
        "name": problem_name,
        "description": desc,
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "points": points,
        "partial": True,
        "group": clue_group,
        "is_public": is_public,
        "is_manually_managed": True,
        "is_clue": True,
        "clue_code": clue_code,
        "date": timezone.now(),
    }

    problem, created = Problem.objects.get_or_create(code=problem_code, defaults=defaults)
    if not created:
        for k, v in defaults.items():
            setattr(problem, k, v)
        problem.save()

    problem.types.add(clue_type)
    problem.allowed_languages.set(Language.objects.all())
    if author_profile:
        problem.authors.add(author_profile)

    # Create data directory with template init.yml
    data_dir = f"/home/dmoj/site/data/{problem_code}"
    os.makedirs(data_dir, exist_ok=True)
    init_yml = os.path.join(data_dir, "init.yml")
    if not os.path.exists(init_yml):
        with open(init_yml, "w", encoding="utf-8") as f:
            f.write(f"# Remote ClueOJ problem: {clue_code}\nchecker: standard\ntest_cases: []\n")

    logger.info(f"Imported ClueOJ problem: {problem_code} ({problem_name})")
    return problem


def import_clue_organization_problem(
    clue_input: str,
    clue_username: str = None,
    clue_password: str = None,
    organization: str = "csattutor",
    code_override: str = None,
    name_override: str = None,
    points_override: str = None,
    time_limit_override: str = None,
    memory_limit_override: str = None,
    is_public: bool = True,
    author_profile = None,
):
    """
    Import a problem from a private ClueOJ organization, downloading all test cases and init.yml,
    extracting them into the problem storage directory so the problem can be judged locally,
    while tagging it as an organization problem and preserving ClueOJ remote links.
    """
    import http.cookiejar
    import io
    import zipfile
    import yaml
    import shutil
    import html as pyhtml
    from judge.models import ProblemData, ProblemTestCase, Profile
    from judge.utils.clue_service import login_clue, get_profile_clue_opener, check_clue_login, CLUE_BASE_URL, HEADERS

    clue_code = parse_clue_id(clue_input)
    if not clue_code:
        raise ValueError(f"Không thể nhận diện mã bài ClueOJ từ '{clue_input}'. Ví dụ: csattutor_tmath_n0216d")

    # 1. Authenticate with ClueOJ
    opener = None
    active_cookie = None

    if clue_username and clue_password:
        res = login_clue(clue_username.strip(), clue_password.strip())
        if res.get("success"):
            active_cookie = res["cookie"]
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            if author_profile:
                author_profile.clue_username = clue_username.strip()
                author_profile.clue_password = clue_password.strip()
                author_profile.clue_cookie = active_cookie
                author_profile.save(update_fields=["clue_username", "clue_password", "clue_cookie"])

    if not opener and author_profile:
        opener, active_cookie = get_profile_clue_opener(author_profile)

    if not opener:
        # Fallback to any profile in DB with clue credentials or default csattutor
        fallback = Profile.objects.filter(clue_username__gt='').first()
        if fallback:
            opener, active_cookie = get_profile_clue_opener(fallback)

    if not opener:
        # Fallback to default credentials provided by user
        res = login_clue("csattutor", "vicsatlanha")
        if res.get("success"):
            active_cookie = res["cookie"]
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            if author_profile:
                author_profile.clue_username = "csattutor"
                author_profile.clue_password = "vicsatlanha"
                author_profile.clue_cookie = active_cookie
                author_profile.save(update_fields=["clue_username", "clue_password", "clue_cookie"])

    if not opener:
        raise ValueError("Không thể đăng nhập vào tài khoản ClueOJ. Vui lòng kiểm tra lại tên đăng nhập và mật khẩu.")

    headers = dict(HEADERS)
    if active_cookie:
        headers["Cookie"] = active_cookie

    # 2. Fetch problem statement page
    prob_url = f"{CLUE_BASE_URL}/problem/{clue_code}"
    try:
        req = urllib.request.Request(prob_url, headers=headers)
        with opener.open(req, timeout=15) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        raise ValueError(f"Không thể tải đề bài {clue_code} từ ClueOJ: {e}")

    # Title
    t_m = re.search(r'<div class="problem-title-group">\s*<h2>(.*?)</h2>', html_text)
    if not t_m:
        t_m = re.search(r'<h2[^>]*>(.*?)</h2>', html_text)
    clue_title = t_m.group(1).strip() if t_m else clue_code
    clue_title = re.sub(r'<[^>]+>', '', clue_title).strip()

    # Problem Code
    if code_override:
        problem_code = clean_problem_code(code_override)
    else:
        problem_code = clean_problem_code(clue_code)

    # Problem Name
    problem_name = name_override.strip() if name_override else f"ClueOJ - {clue_code}: {clue_title}"
    if len(problem_name) > 100:
        problem_name = problem_name[:97] + "..."

    # Time & Memory Limits
    time_limit = 1.0
    if time_limit_override:
        try:
            time_limit = float(time_limit_override)
        except (ValueError, TypeError):
            pass
    else:
        tl_m = re.search(r'(\d+(\.\d+)?)\s*s', html_text)
        if tl_m:
            try:
                time_limit = float(tl_m.group(1))
            except Exception:
                pass
    time_limit = max(0.1, min(60.0, time_limit))

    memory_limit = 262144
    if memory_limit_override:
        try:
            memory_limit = int(memory_limit_override)
        except (ValueError, TypeError):
            pass
    else:
        mem_m = re.search(r'(\d+)\s*(MB|M|K|KB)', html_text, re.I)
        if mem_m:
            try:
                val = int(mem_m.group(1))
                unit = mem_m.group(2).lower()
                memory_limit = val * 1024 if 'm' in unit else val
            except Exception:
                pass
    memory_limit = max(4096, min(2097152, memory_limit))

    points = 100.0
    if points_override:
        try:
            points = float(points_override)
        except (ValueError, TypeError):
            pass
    else:
        pts_m = re.search(r'(\d+)\s*(điểm|pts|points)', html_text, re.I)
        if pts_m:
            try:
                points = float(pts_m.group(1))
            except Exception:
                pass

    # 3. Description cleanup
    pdf_m = re.search(r'https://oj\.clue\.edu\.vn/pdf/[a-zA-Z0-9_-]+\.pdf', html_text)
    if not pdf_m:
        pdf_m = re.search(r'/pdf/[a-zA-Z0-9_-]+\.pdf', html_text)
        pdf_url = f"{CLUE_BASE_URL}{pdf_m.group(0)}" if pdf_m else None
    else:
        pdf_url = pdf_m.group(0)

    desc_m = re.search(r'<div class="content-description screen">([\s\S]*?)</div>\s*<hr>', html_text)
    if not desc_m:
        desc_m = re.search(r'<div class="content-description screen">([\s\S]*?)</div>', html_text)
    raw_desc = desc_m.group(1).strip() if desc_m else ""
    raw_desc = re.sub(r'<iframe[\s\S]*?</iframe>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'<object[\s\S]*?</object>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'<embed[\s\S]*?>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'<p>\s*Trong trường hợp đề bài hiển thị không chính xác[\s\S]*?</p>', '', raw_desc, flags=re.I)
    raw_desc = re.sub(r'href="/(.*?)"', rf'href="{CLUE_BASE_URL}/\1"', raw_desc)
    raw_desc = re.sub(r'src="/(.*?)"', rf'src="{CLUE_BASE_URL}/\1"', raw_desc)
    raw_desc = re.sub(r'data="/(.*?)"', rf'data="{CLUE_BASE_URL}/\1"', raw_desc)
    # Strip unnecessary outer <div> tags that could unbalance DOM
    if raw_desc.strip().startswith('<div>') and raw_desc.count('<div') > raw_desc.count('</div>'):
        raw_desc = raw_desc.strip()[5:].strip()
    clean_desc = clean_vjudge_math(raw_desc)
    pdf_marker = f"<!-- CLUE_PDF:{pdf_url} -->\n" if pdf_url else ""
    desc = f"{pdf_marker}{clean_desc}".strip()

    # 4. Fetch test data page & download ZIP
    test_data_url = f"{CLUE_BASE_URL}/problem/{clue_code}/test_data"
    test_html = ""
    try:
        req_test = urllib.request.Request(test_data_url, headers=headers)
        with opener.open(req_test, timeout=15) as resp:
            test_html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"Failed to fetch test_data page for {clue_code}: {e}")

    zip_bytes = None
    zip_filename = "tests.zip"
    zip_link_m = re.search(r'href="(/problem/[^/]+/data/([^"]+\.zip))"', test_html)
    if zip_link_m:
        zip_path = zip_link_m.group(1)
        zip_filename = zip_link_m.group(2)
        zip_download_url = f"{CLUE_BASE_URL}{zip_path}"
        try:
            req_zip = urllib.request.Request(zip_download_url, headers=headers)
            with opener.open(req_zip, timeout=30) as r_zip:
                zip_bytes = r_zip.read()
            logger.info(f"Downloaded tests zip for {clue_code}: {len(zip_bytes)} bytes")
        except Exception as e:
            logger.warning(f"Failed to download tests zip from {zip_download_url}: {e}")

    # 5. Fetch or construct init.yml
    raw_yaml = None
    init_url = f"{CLUE_BASE_URL}/problem/{clue_code}/test_data/init"
    try:
        req_init = urllib.request.Request(init_url, headers=headers)
        with opener.open(req_init, timeout=12) as r_init:
            init_html = r_init.read().decode("utf-8", errors="ignore")
            m_code = re.search(r'<div class="codehilite">.*?<code>(.*?)</code>', init_html, re.DOTALL)
            if m_code:
                raw_yaml = pyhtml.unescape(re.sub(r'<[^>]+>', '', m_code.group(1))).strip()
    except Exception as e:
        logger.warning(f"Failed to fetch /test_data/init for {clue_code}: {e}")

    # 6. Save test files to disk
    primary_data_dir = os.path.join(getattr(settings, "DMOJ_PROBLEM_DATA_ROOT", "/home/dmoj/problems"), problem_code)
    alt_data_dir = os.path.join("/home/dmoj/problems", problem_code)
    dirs_to_save = list(set([primary_data_dir, alt_data_dir, f"/home/dmoj/site/data/{problem_code}"]))

    parsed_test_cases = []
    if raw_yaml:
        try:
            parsed_data = yaml.safe_load(raw_yaml)
            if isinstance(parsed_data, dict) and "test_cases" in parsed_data:
                parsed_test_cases = parsed_data["test_cases"]
        except Exception:
            pass

    for d in dirs_to_save:
        try:
            os.makedirs(d, exist_ok=True)
            if zip_bytes:
                zip_path_disk = os.path.join(d, zip_filename)
                with open(zip_path_disk, "wb") as f_zip:
                    f_zip.write(zip_bytes)
                # Extract archive into directory so individual files are directly accessible
                try:
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                        zf.extractall(d)
                except Exception as e:
                    logger.warning(f"Failed to extract zip in {d}: {e}")

            init_path_disk = os.path.join(d, "init.yml")
            if raw_yaml:
                with open(init_path_disk, "w", encoding="utf-8") as f_init:
                    f_init.write(raw_yaml)
            elif not os.path.exists(init_path_disk):
                with open(init_path_disk, "w", encoding="utf-8") as f_init:
                    f_init.write(f"# Problem: {clue_code}\nchecker: standard\ntest_cases: []\n")
        except Exception as e:
            logger.warning(f"Error saving files to {d}: {e}")

    # 7. Save to Database
    clue_group, _ = ProblemGroup.objects.get_or_create(
        name="ClueOJ",
        defaults={"full_name": "Clue Online Judge"}
    )
    clue_type, _ = ProblemType.objects.get_or_create(
        name="remote",
        defaults={"full_name": "Remote OJ"}
    )

    defaults = {
        "name": problem_name,
        "description": desc,
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "points": points,
        "partial": True,
        "group": clue_group,
        "is_public": is_public,
        "is_manually_managed": False, # Local judge can grade it!
        "is_clue": True,
        "clue_code": clue_code,
        "is_clue_org": True,
        "clue_organization": organization.strip() or "csattutor",
        "date": timezone.now(),
    }

    problem, created = Problem.objects.get_or_create(code=problem_code, defaults=defaults)
    if not created:
        for k, v in defaults.items():
            setattr(problem, k, v)
        problem.save()

    problem.types.add(clue_type)
    problem.allowed_languages.set(Language.objects.all())
    if author_profile:
        problem.authors.add(author_profile)

    # ProblemData
    if zip_filename:
        problem_data, _ = ProblemData.objects.get_or_create(problem=problem)
        problem_data.zipfile.name = f"{problem_code}/{zip_filename}"
        problem_data.checker = 'standard'
        problem_data.save()

    # ProblemTestCase
    if parsed_test_cases:
        ProblemTestCase.objects.filter(dataset=problem).delete()
        test_case_objs = []
        for idx, tc in enumerate(parsed_test_cases, 1):
            test_case_objs.append(ProblemTestCase(
                dataset=problem,
                order=idx,
                type='C',
                input_file=tc.get('in', ''),
                output_file=tc.get('out', ''),
                points=float(tc.get('points', 1.0)),
                is_pretest=False,
                checker='standard',
            ))
        ProblemTestCase.objects.bulk_create(test_case_objs)

    # 8. Reload judge in background so it detects new problem
    try:
        import subprocess
        subprocess.Popen(["sudo", "supervisorctl", "restart", "judge"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    logger.info(f"Imported ClueOJ Organization problem: {problem_code} with {len(parsed_test_cases)} tests")
    return problem, len(parsed_test_cases)
