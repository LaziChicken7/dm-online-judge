import logging
import re
import urllib.request
import json
from django.conf import settings
from django.utils import timezone
from judge.models import Language, Problem, ProblemGroup, ProblemType
from judge.utils.vjudge_service import (
    clean_vjudge_math,
    get_vjudge_problem_data,
    get_vjudge_statement_content,
    select_best_vjudge_statement,
)

logger = logging.getLogger('judge.vjudge_importer')


def parse_vjudge_id(text: str):
    """
    Extract (remote_oj, prob_num) from URL or string.
    Examples:
      - 'https://vjudge.net/problem/CodeForces-1100F' -> ('CodeForces', '1100F')
      - 'CodeForces-1100F' -> ('CodeForces', '1100F')
      - 'POJ-2251' -> ('POJ', '2251')
    """
    text = (text or '').strip()
    m = re.search(r'vjudge\.net/problem/([A-Za-z0-9_]+)-([A-Za-z0-9_]+)', text)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'^([A-Za-z0-9_]+)[-:\s/]([A-Za-z0-9_]+)$', text)
    if m:
        return m.group(1), m.group(2)
    return None, None


def clean_problem_code(raw: str, fallback: str = "vjudge") -> str:
    cleaned = re.sub(r'[^a-z0-9]', '', raw.lower())
    if not cleaned:
        cleaned = re.sub(r'[^a-z0-9]', '', fallback.lower())
    if not cleaned:
        cleaned = "vjudge"
    return cleaned[:20]


def fetch_vjudge_problem_info(oj: str, prob_num: str):
    """
    Calls https://vjudge.net/problem/info/{oj}-{prob_num} to get PID and title.
    """
    url = f"https://vjudge.net/problem/info/{oj}-{prob_num}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
            return data
    except Exception as e:
        logger.warning(f"Failed to fetch info for {oj}-{prob_num} from VJudge: {e}")
        return None


def import_vjudge_problem(
    vjudge_input: str,
    code_override: str = None,
    name_override: str = None,
    points_override: str = None,
    time_limit_override: str = None,
    memory_limit_override: str = None,
    is_public: bool = True,
    author_profile = None,
):
    oj, prob_num = parse_vjudge_id(vjudge_input)
    if not oj or not prob_num:
        raise ValueError(
            f"Cannot parse VJudge problem from '{vjudge_input}'. Expected format: CodeForces-1100F or https://vjudge.net/problem/CodeForces-1100F"
        )

    # 1. Fetch info from VJudge
    info = fetch_vjudge_problem_info(oj, prob_num) or {}
    pid = info.get('pid')
    vjudge_title = info.get('title') or f"{oj} {prob_num}"
    vjudge_title = clean_vjudge_math(vjudge_title)

    # 2. Problem Code
    if code_override:
        problem_code = clean_problem_code(code_override)
    else:
        # Generate clean short code: e.g. cf1100f or poj2251
        oj_short = oj.lower()
        if oj_short == 'codeforces':
            oj_short = 'cf'
        elif oj_short == 'atcoder':
            oj_short = 'atc'
        problem_code = clean_problem_code(f"{oj_short}{prob_num}")

    # 3. Problem Name
    problem_name = name_override if name_override else f"{oj} - {prob_num}: {vjudge_title}"
    problem_name = clean_vjudge_math(problem_name)
    if len(problem_name) > 100:
        problem_name = problem_name[:97] + "..."

    # 4. Limits & Points
    try:
        time_limit = float(time_limit_override) if time_limit_override else 2.0
    except (ValueError, TypeError):
        time_limit = 2.0
    time_limit = max(0.1, min(60.0, time_limit))

    try:
        memory_limit = int(memory_limit_override) if memory_limit_override else 262144
    except (ValueError, TypeError):
        memory_limit = 262144
    memory_limit = max(4096, min(2097152, memory_limit))

    try:
        points = float(points_override) if points_override else 100.0
    except (ValueError, TypeError):
        points = 100.0

    # 5. Problem Group & Type
    vjudge_group, _ = ProblemGroup.objects.get_or_create(
        name="VJudge",
        defaults={"full_name": "Virtual Judge Remote Problems"}
    )
    vjudge_type, _ = ProblemType.objects.get_or_create(
        name="remote",
        defaults={"full_name": "Remote OJ"}
    )

    # Description
    desc = f"""
## {problem_name}

*Đề bài được nhập từ Virtual Judge: [{oj} - {prob_num}](https://vjudge.net/problem/{oj}-{prob_num})*

---
    """.strip()

    # Try to fetch default statement content
    try:
        prob_data = get_vjudge_problem_data(oj, prob_num)
        if prob_data and prob_data.get('descBriefs'):
            best_stmt = select_best_vjudge_statement(prob_data.get('descBriefs'), user_lang="vi")
            if best_stmt and best_stmt.get('key'):
                stmt_content = get_vjudge_statement_content(str(best_stmt['key']))
                if stmt_content:
                    desc = f"{desc}\n\n{stmt_content}"
    except Exception as e:
        logger.warning(f"Failed to fetch initial description for {oj}-{prob_num}: {e}")

    defaults = {
        "name": problem_name,
        "description": desc,
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "points": points,
        "partial": False,
        "group": vjudge_group,
        "is_public": is_public,
        "is_manually_managed": True,  # Prevent judge daemon from demanding local test cases
        "is_vjudge": True,
        "vjudge_oj": oj,
        "vjudge_prob_num": prob_num,
        "vjudge_pid": pid,
        "date": timezone.now(),
    }

    problem, created = Problem.objects.get_or_create(code=problem_code, defaults=defaults)
    if not created:
        for k, v in defaults.items():
            setattr(problem, k, v)
        problem.save()

    problem.types.add(vjudge_type)
    # Allow all available languages
    problem.allowed_languages.set(Language.objects.all())

    if author_profile:
        problem.authors.add(author_profile)

    return problem, created
