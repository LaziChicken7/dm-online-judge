import logging
import time
import threading
import re
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('judge.vjudge_judge')


def format_vjudge_error(raw_err, oj="CodeForces"):
    """
    Convert raw VJudge error object or string into clear, human-friendly message.
    """
    if not raw_err:
        return "Không thể gửi bài lên Virtual Judge (lỗi không xác định)."

    err_str = str(raw_err)
    i18n_key = ''
    error_code = ''
    if isinstance(raw_err, dict):
        error_val = raw_err.get('error')
        if isinstance(error_val, dict):
            i18n_key = error_val.get('i18nKey', '')
        elif isinstance(error_val, str):
            i18n_key = error_val
        error_code = raw_err.get('errorCode', '')
        if not i18n_key:
            i18n_key = raw_err.get('i18nKey', '')

    if 'login_required' in err_str or 'login_required' in i18n_key:
        return (
            "Phiên đăng nhập Virtual Judge chưa được kết nối hoặc đã hết hạn (Cookie JSESSIONID). "
            "Vui lòng vào mục 'Kết nối Virtual Judge' (https://omnijudge.id.vn/user/vjudge/connect/) "
            "để cập nhật lại Cookie phiên làm việc mới từ vjudge.net trước khi nộp bài."
        )
    if 'illegal_language' in err_str or 'illegal_language' in i18n_key:
        return (
            f"Trình biên dịch đã chọn không được Virtual Judge hoặc {oj} hỗ trợ cho đề bài này. "
            f"Hệ thống đã tự động cập nhật bảng mã ngôn ngữ mới nhất."
        )

    if 'duplicate_code' in err_str or 'duplicate_code' in i18n_key:
        return (
            f"CodeForces / Virtual Judge từ chối bài nộp vì mã nguồn trùng lặp hoàn toàn với bài vừa nộp gần đây. "
            f"Vui lòng thêm một dòng comment hoặc chỉnh sửa nhỏ trong mã nguồn trước khi nộp lại."
        )

    if 'submit_method_not_allowed' in err_str or 'submit_method_not_allowed' in i18n_key:
        return (
            f"Virtual Judge không cho phép nộp bằng tài khoản robot mặc định cho {oj}. "
            f"Bạn cần nộp bằng tài khoản {oj} cá nhân (Own Account). Vui lòng kiểm tra tài khoản liên kết trên VJudge."
        )

    if 'bind_account_missing' in err_str or 'submit.own_account.error.missing' in err_str or error_code == 'bind_account_missing':
        return (
            f"Tài khoản {oj} trên Virtual Judge chưa hoàn tất liên kết (INCOMPLETE / MISSING_BINDING) hoặc chưa có tài khoản. "
            f"Vui lòng mở Virtual Judge -> Manage Accounts (https://vjudge.net/user/remoteAccounts?oj={oj}) để nhập mật khẩu hoặc liên kết tài khoản {oj} trước khi nộp."
        )

    if 'account_disabled' in err_str or 'account_locked' in err_str:
        return f"Tài khoản {oj} liên kết trên Virtual Judge đang bị khóa hoặc không thể đăng nhập."

    if 'invalid_credentials' in err_str:
        return f"Thông tin đăng nhập tài khoản {oj} trên Virtual Judge không chính xác."

    if 'human_verification' in err_str or 'turnstile' in err_str:
        return "Virtual Judge yêu cầu xác minh bảo mật (Turnstile/Captcha). Vui lòng đăng nhập trực tiếp trên vjudge.net."

    return f"Lỗi từ Virtual Judge: {err_str}"


def resolve_vjudge_language(oj: str, dmoj_lang_key: str, available_languages: dict = None) -> str:
    """
    Resolve DMOJ language key (e.g. 'CPP20', 'PY3') to a valid VJudge compiler ID.
    If available_languages from problem data is provided, match against them dynamically.
    """
    key = (dmoj_lang_key or '').upper()
    oj_upper = (oj or '').upper()

    # 1. Dynamic matching if problem has available_languages map
    if available_languages and isinstance(available_languages, dict):
        search_terms = []
        if key in ('CPP20', 'C++20'):
            search_terms = ['g++20', 'c++20', 'g++23', 'c++23', 'g++17', 'c++17', 'g++']
        elif key in ('CPP23', 'C++23'):
            search_terms = ['g++23', 'c++23', 'g++20', 'c++20', 'g++17', 'c++17', 'g++']
        elif key in ('CPP17', 'C++17'):
            search_terms = ['g++17', 'c++17', 'g++20', 'c++20', 'g++']
        elif 'CPP' in key or 'C++' in key:
            search_terms = ['g++20', 'c++20', 'g++17', 'c++17', 'g++']
        elif key in ('PY3', 'PYTHON3'):
            search_terms = ['python 3', 'python3', 'pypy 3', 'pypy3']
        elif key in ('PY2', 'PYTHON2'):
            search_terms = ['python 2', 'python2', 'pypy 2', 'pypy2']
        elif key in ('PYPY3',):
            search_terms = ['pypy 3', 'pypy3', 'python 3']
        elif key in ('C', 'C11', 'C99'):
            search_terms = ['gcc c11', 'gcc c', 'gnu gcc c', 'c11', 'clang c']
        elif 'JAVA' in key:
            search_terms = ['java 21', 'java 17', 'java 8', 'java']
        elif 'RUST' in key:
            search_terms = ['rust 1', 'rust']
        elif 'GO' in key:
            search_terms = ['go 1', 'go']
        elif 'PAS' in key:
            search_terms = ['pascal', 'free pascal']

        for term in search_terms:
            for lid, lname in available_languages.items():
                if term in lname.lower():
                    return str(lid)

    # 2. Modern static mappings by OJ
    if oj_upper in ('VNOJ', 'VNOI'):
        vnoj_map = {
            'CPP23': '23',
            'CPP20': '14',
            'CPP17': '4',
            'CPP14': '3',
            'CPP11': '2',
            'CPP03': '1',
            'C': '5',
            'C11': '6',
            'PY3': '9',
            'PY2': '8',
            'PYPY3': '17',
            'PYPY2': '16',
            'JAVA': '18',
            'JAVA8': '10',
            'RUST': '22',
            'GO': '21',
            'PAS': '7',
        }
        if key in vnoj_map:
            return vnoj_map[key]
        if 'CPP' in key or 'C++' in key:
            return '14'
    elif oj_upper == 'CODEFORCES':
        cf_map = {
            'CPP23': '91',  # GNU G++23 14.2
            'CPP20': '89',  # GNU G++20 13.2
            'CPP17': '54',  # GNU G++17 7.3.0
            'CPP14': '89',
            'CPP11': '89',
            'CPP03': '89',
            'C': '43',      # GNU GCC C11 5.1.0
            'C11': '43',    # GNU GCC C11 5.1.0
            'PY3': '31',    # Python 3.13.2
            'PY2': '7',     # Python 2.7.18
            'PYPY3': '70',  # PyPy 3.10
            'JAVA8': '36',  # Java 8 32bit
            'JAVA': '87',   # Java 21 64bit
            'RUST': '75',   # Rust 1.89
            'GO': '32',     # Go 1.22.2
            'PAS': '4',     # Free Pascal 3.2.2
        }
        if key in cf_map:
            return cf_map[key]
        if 'CPP' in key or 'C++' in key:
            return '89'

    # Fallback to 89 (modern C++) or 54
    if 'CPP' in key or 'C++' in key:
        return '89'
    return '89'


@shared_task(name='judge.tasks.submission.judge_vjudge_submission_task')
def judge_vjudge_submission_task(submission_id, method=0, binding_id=None, open_code=1):
    from judge.models import Submission, SubmissionTestCase
    from judge.judgeapi import _post_update_submission
    from judge import event_poster as event
    from judge.utils.vjudge_service import submit_vjudge_solution, poll_vjudge_submission, get_vjudge_problem_data

    try:
        submission = Submission.objects.select_related('problem', 'user', 'language', 'source').get(id=submission_id)
    except Submission.DoesNotExist:
        logger.error(f"Submission {submission_id} does not exist")
        return False

    problem = submission.problem
    profile = submission.user

    # 1. Update status to Processing
    Submission.objects.filter(id=submission_id).update(status='P')
    event.post(f'sub_{submission.id_secret}', {'type': 'processing'})
    _post_update_submission(submission)

    # 2. Dynamic Language Mapping
    from judge.utils.vjudge_service import get_any_vjudge_cookie
    cookie = profile.vjudge_cookie or get_any_vjudge_cookie() or ""
    prob_data = get_vjudge_problem_data(problem.vjudge_oj, problem.vjudge_prob_num, cookie=cookie)
    available_langs = prob_data.get('languages', {}) if prob_data else {}
    vjudge_lang = resolve_vjudge_language(problem.vjudge_oj, submission.language.key, available_langs)

    # 3. Resolve binding_id & method for Own Account
    is_cf = problem.vjudge_oj.lower() == 'codeforces'
    if is_cf:
        method = 1
        if not binding_id:
            if profile.vjudge_binding_id:
                binding_id = profile.vjudge_binding_id
            elif cookie:
                from judge.utils.vjudge_service import get_vjudge_remote_accounts
                accs = get_vjudge_remote_accounts(cookie, "CodeForces")
                for acc in accs:
                    if acc.get('isReady'):
                        binding_id = acc.get('id')
                        break
                if not binding_id and accs:
                    binding_id = accs[0].get('id')
        # Save default binding to profile if found
        if binding_id and profile.vjudge_binding_id != int(binding_id):
            try:
                profile.vjudge_binding_id = int(binding_id)
                profile.save(update_fields=['vjudge_binding_id'])
            except Exception:
                pass
    elif int(method) == 0:
        binding_id = None

    # 4. Submit to VJudge or reuse runId
    run_id = submission.vjudge_run_id
    if not run_id:
        raw_source = submission.source.source
        source_to_send = f"{raw_source}\n// dmoj_sub_{submission_id}"

        logger.info(f"Submitting submission #{submission_id} to VJudge: {problem.vjudge_oj}-{problem.vjudge_prob_num}, lang={vjudge_lang}, method={method}, binding_id={binding_id}")

        res = submit_vjudge_solution(
            oj=problem.vjudge_oj,
            prob_num=problem.vjudge_prob_num,
            language=vjudge_lang,
            source=source_to_send,
            cookie=cookie,
            method=int(method),
            binding_id=int(binding_id) if binding_id else None,
            open_code=int(open_code)
        )

        if not res.get('success'):
            raw_err = res.get('error')
            err_msg = format_vjudge_error(raw_err, oj=problem.vjudge_oj)
            logger.error(f"Submission #{submission_id} VJudge submission error: {err_msg}")
            Submission.objects.filter(id=submission_id).update(status='IE', result='IE', error=err_msg)
            event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
            _post_update_submission(submission, done=True)
            return False

        run_id = res['runId']
        logger.info(f"Submission #{submission_id} received VJudge runId={run_id}")
        Submission.objects.filter(id=submission_id).update(status='G', vjudge_run_id=run_id)
        event.post(f'sub_{submission.id_secret}', {'type': 'grading-begin'})
        _post_update_submission(submission)
    else:
        logger.info(f"Submission #{submission_id} reusing existing VJudge runId={run_id}")
        Submission.objects.filter(id=submission_id).update(status='G')
        event.post(f'sub_{submission.id_secret}', {'type': 'grading-begin'})
        _post_update_submission(submission)

    # 5. Polling for verdict (max 60 tries * 3s = 180s)
    max_retries = 60
    final_data = None
    for attempt in range(max_retries):
        time.sleep(3)
        poll_res = poll_vjudge_submission(
            run_id=run_id,
            username=profile.vjudge_username,
            oj=problem.vjudge_oj,
            prob_num=problem.vjudge_prob_num,
            cookie=cookie
        )
        logger.debug(f"Poll #{attempt} for runId {run_id}: done={poll_res.get('done')}, status={poll_res.get('status')}")
        if poll_res.get('done'):
            final_data = poll_res
            break

    if not final_data:
        logger.warning(f"Submission #{submission_id} VJudge polling timed out")
        Submission.objects.filter(id=submission_id).update(status='IE', result='IE', error='VJudge polling timed out after 3 minutes')
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    vjudge_status_orig = (final_data.get('status') or '').strip()
    status_lower = vjudge_status_orig.lower()
    logger.info(f"Submission #{submission_id} finished on VJudge with status: {vjudge_status_orig}")

    # 1. Check additional_info for test counts (e.g. "21.0 / 21.0" or "15.0 / 21.0" from VNOJ/DMOJ forks)
    add_info_str = str(final_data.get('additional_info') or '')
    score_match = re.search(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)', add_info_str)

    # 2. Extract test case number from VJudge status (e.g. "Wrong answer on test 5" -> 5)
    test_match = re.search(r'on\s+test\s+(\d+)', vjudge_status_orig, re.IGNORECASE)
    failed_test_num = int(test_match.group(1)) if test_match else None

    # 6. Map VJudge verdict to DMOJ result
    if 'accept' in status_lower:
        verdict = 'AC'
        points = problem.points
        status = 'D'
    elif 'wrong' in status_lower:
        verdict = 'WA'
        points = 0.0
        status = 'D'
    elif 'time' in status_lower:
        verdict = 'TLE'
        points = 0.0
        status = 'D'
    elif 'memory' in status_lower:
        verdict = 'MLE'
        points = 0.0
        status = 'D'
    elif 'output' in status_lower:
        verdict = 'OLE'
        points = 0.0
        status = 'D'
    elif 'runtime' in status_lower:
        verdict = 'RTE'
        points = 0.0
        status = 'D'
    elif 'compil' in status_lower:
        verdict = 'CE'
        points = 0.0
        status = 'CE'
    else:
        verdict = 'WA'
        points = 0.0
        status = 'D'

    time_sec = round((final_data.get('runtime') or 0) / 1000.0, 3)
    memory_kb = round(final_data.get('memory') or 0, 1)
    err_text = final_data.get('additional_info') or ''

    # Clean existing test cases if rejudging
    SubmissionTestCase.objects.filter(submission=submission).delete()

    case_points = 0.0
    case_total = 0.0

    if score_match:
        passed_num = int(round(float(score_match.group(1))))
        total_num = int(round(float(score_match.group(2))))
        if total_num > 0:
            case_total = float(total_num)
            case_points = float(passed_num)
            cases_to_create = []
            per_case_time = round(time_sec / max(1, passed_num), 3) if (time_sec and passed_num) else 0.004
            for c in range(1, total_num + 1):
                if c <= passed_num:
                    cases_to_create.append(SubmissionTestCase(
                        submission=submission,
                        case=c,
                        status='AC',
                        points=1.0,
                        total=1.0,
                        time=per_case_time,
                        memory=memory_kb,
                        feedback='Accepted'
                    ))
                else:
                    cases_to_create.append(SubmissionTestCase(
                        submission=submission,
                        case=c,
                        status=verdict if c == passed_num + 1 else 'WA',
                        points=0.0,
                        total=1.0,
                        time=time_sec,
                        memory=memory_kb,
                        feedback=vjudge_status_orig
                    ))
            SubmissionTestCase.objects.bulk_create(cases_to_create)
    elif failed_test_num and failed_test_num > 0:
        case_total = float(failed_test_num)
        case_points = float(failed_test_num - 1)
        cases_to_create = []
        for c in range(1, failed_test_num):
            cases_to_create.append(SubmissionTestCase(
                submission=submission,
                case=c,
                status='AC',
                points=1.0,
                total=1.0,
                feedback='Passed'
            ))
        cases_to_create.append(SubmissionTestCase(
            submission=submission,
            case=failed_test_num,
            status=verdict,
            points=0.0,
            total=1.0,
            time=time_sec,
            memory=memory_kb,
            feedback=vjudge_status_orig
        ))
        SubmissionTestCase.objects.bulk_create(cases_to_create)
    elif verdict == 'AC':
        case_points = 1.0
        case_total = 1.0
        SubmissionTestCase.objects.create(
            submission=submission,
            case=1,
            status='AC',
            points=1.0,
            total=1.0,
            time=time_sec,
            memory=memory_kb,
            feedback='Accepted'
        )
    elif verdict in ('WA', 'TLE', 'MLE', 'RTE', 'OLE'):
        case_total = 1.0
        case_points = 0.0
        SubmissionTestCase.objects.create(
            submission=submission,
            case=1,
            status=verdict,
            points=0.0,
            total=1.0,
            time=time_sec,
            memory=memory_kb,
            feedback=vjudge_status_orig
        )

    # 7. Save final result in DMOJ database
    Submission.objects.filter(id=submission_id).update(
        status=status,
        result=verdict,
        points=points,
        case_points=case_points,
        case_total=case_total,
        time=time_sec,
        memory=memory_kb,
        error=err_text,
        judged_date=timezone.now(),
    )

    submission.refresh_from_db()
    submission.update_contest()
    profile.calculate_points()

    # 8. Broadcast live websocket event
    event.post(f'sub_{submission.id_secret}', {
        'type': 'done',
        'result': verdict,
        'points': points,
        'case_points': case_points,
        'case_total': case_total,
        'time': time_sec,
        'memory': memory_kb,
        'status_text': vjudge_status_orig,
    })
    _post_update_submission(submission, done=True)
    return True


def judge_vjudge_submission_async(submission, method=0, binding_id=None, open_code=1):
    """
    Dispatch VJudge judging task via Celery, falling back to background thread.
    """
    sub_id = submission.id
    try:
        judge_vjudge_submission_task.delay(sub_id, method, binding_id, open_code)
        logger.info(f"Dispatched Celery task for submission #{sub_id}")
    except Exception as e:
        logger.warning(f"Failed to dispatch Celery task ({e}), falling back to background thread")
        t = threading.Thread(
            target=judge_vjudge_submission_task,
            args=(sub_id, method, binding_id, open_code),
            daemon=True
        )
        t.start()
