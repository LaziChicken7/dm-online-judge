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
    return cleaned[:20]

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
