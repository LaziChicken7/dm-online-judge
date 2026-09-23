import logging
import os
import re
from django.conf import settings
from django.utils import timezone
from judge.models import Language, Problem, ProblemGroup, ProblemType
from judge.utils.ntucoder_service import get_ntucoder_problem_data

logger = logging.getLogger('judge.ntucoder_importer')

def clean_problem_code(raw: str, fallback: str = "ntu") -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_]', '', raw.lower())
    if not cleaned:
        cleaned = re.sub(r'[^a-zA-Z0-9_]', '', fallback.lower())
    if not cleaned:
        cleaned = "ntu"
    return cleaned[:100]

def import_ntucoder_problem(
    ntucoder_input: str,
    code_override: str = None,
    name_override: str = None,
    points_override: str = None,
    time_limit_override: str = None,
    memory_limit_override: str = None,
    is_public: bool = True,
    author_profile = None,
):
    """
    Import or update a problem from NTUCoder (thptchuyen.ntucoder.net).
    """
    prob_data = get_ntucoder_problem_data(ntucoder_input)
    if not prob_data:
        raise ValueError(
            f"Không tìm thấy bài tập NTUCoder từ '{ntucoder_input}'. "
            f"Vui lòng nhập ID bài (ví dụ: 11042) hoặc đường dẫn đầy đủ "
            f"(ví dụ: https://thptchuyen.ntucoder.net/Problem/Details/11042)"
        )

    ntucoder_id = prob_data["problem_id"]
    ntucoder_code = prob_data["problem_code"]
    problem_title = prob_data["problem_title"]
    full_name = prob_data["full_name"]
    original_url = prob_data["original_url"]

    # 1. Problem Code
    if code_override:
        problem_code = clean_problem_code(code_override)
    else:
        problem_code = clean_problem_code(f"ntu_{ntucoder_code}")

    # 2. Problem Name
    import html as pyhtml
    problem_name = name_override.strip() if name_override else pyhtml.unescape(f"NTUCoder - {ntucoder_code}: {problem_title}")
    if len(problem_name) > 100:
        problem_name = problem_name[:97] + "..."

    # 3. Limits & Points
    time_limit = prob_data["time_limit"]
    if time_limit_override:
        try:
            time_limit = float(time_limit_override)
        except (ValueError, TypeError):
            pass
    time_limit = max(0.1, min(60.0, time_limit))

    memory_limit = prob_data["memory_limit"]
    if memory_limit_override:
        try:
            memory_limit = int(memory_limit_override)
        except (ValueError, TypeError):
            pass
    memory_limit = max(4096, min(2097152, memory_limit))

    points = 100.0
    if points_override:
        try:
            points = float(points_override)
        except (ValueError, TypeError):
            pass

    # 4. Group & Type
    ntu_group, _ = ProblemGroup.objects.get_or_create(
        name="NTUCoder",
        defaults={"full_name": "THPT Chuyên NTUCoder"}
    )
    remote_type, _ = ProblemType.objects.get_or_create(
        name="remote",
        defaults={"full_name": "Remote OJ"}
    )

    # 5. Clean HTML Description
    clean_html = prob_data["html"]
    desc = f"<!-- NTUCODER_ORIGIN:{original_url} -->\n{clean_html}".strip()

    defaults = {
        "name": problem_name,
        "description": desc,
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "points": points,
        "partial": True,
        "group": ntu_group,
        "is_public": is_public,
        "is_manually_managed": True,
        "is_ntucoder": True,
        "ntucoder_id": ntucoder_id,
        "ntucoder_code": ntucoder_code,
        "date": timezone.now(),
    }

    problem, created = Problem.objects.get_or_create(code=problem_code, defaults=defaults)
    if not created:
        for k, v in defaults.items():
            setattr(problem, k, v)
        problem.save()

    problem.types.add(remote_type)
    problem.allowed_languages.set(Language.objects.all())
    if author_profile:
        problem.authors.add(author_profile)

    # Create data directory with template init.yml
    data_dir = f"/home/dmoj/site/data/{problem_code}"
    os.makedirs(data_dir, exist_ok=True)
    init_yml = os.path.join(data_dir, "init.yml")
    if not os.path.exists(init_yml):
        with open(init_yml, "w", encoding="utf-8") as f:
            f.write(f"# Remote NTUCoder problem: {ntucoder_code} (ID: {ntucoder_id})\nchecker: standard\ntest_cases: []\n")

    logger.info(f"Imported NTUCoder problem: {problem_code} ({problem_name})")
    return problem
