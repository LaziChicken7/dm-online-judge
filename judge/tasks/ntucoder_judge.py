import logging
import time
from celery import shared_task
from django.utils import timezone
from judge import event_poster as event
from judge.models import Submission, SubmissionTestCase
from judge.judgeapi import _post_update_submission
from judge.utils.ntucoder_service import check_ntucoder_login, login_ntucoder, poll_ntucoder_submission, submit_ntucoder_solution

logger = logging.getLogger('judge.ntucoder_judge')


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def judge_ntucoder_submission_task(self, submission_id: int):
    try:
        submission = Submission.objects.select_related('problem', 'user').get(id=submission_id)
    except Submission.DoesNotExist:
        logger.error(f"Submission {submission_id} does not exist for NTUCoder judge.")
        return False

    problem = submission.problem
    profile = submission.user

    if not problem.is_ntucoder:
        logger.error(f"Problem {problem.code} is not an NTUCoder problem.")
        return False

    # 1. Notify grading begin
    submission.status = 'QU'
    submission.save(update_fields=['status'])
    event.post(f'sub_{submission.id_secret}', {'type': 'grading-begin'})
    _post_update_submission(submission)

    # 2. Check & ensure NTUCoder login session
    cookie = profile.ntucoder_cookie
    is_valid = False
    if cookie:
        check = check_ntucoder_login(cookie)
        is_valid = check.get("logged_in", False)

    if not is_valid and profile.ntucoder_email and profile.ntucoder_password:
        res = login_ntucoder(profile.ntucoder_email, profile.ntucoder_password)
        if res.get("success"):
            cookie = res["cookie"]
            profile.ntucoder_cookie = cookie
            profile.ntucoder_username = res.get("username") or profile.ntucoder_username
            profile.save(update_fields=["ntucoder_cookie", "ntucoder_username"])
            is_valid = True

    if not is_valid or not cookie:
        submission.status = 'IE'
        submission.result = 'IE'
        submission.error = "Chưa kết nối hoặc phiên đăng nhập NTUCoder đã hết hạn. Vui lòng kết nối lại tài khoản NTUCoder của bạn."
        submission.save(update_fields=['status', 'result', 'error'])
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    # 3. Submit solution to NTUCoder
    sub_res = submit_ntucoder_solution(
        cookie=cookie,
        problem_id=problem.ntucoder_id,
        problem_code=problem.ntucoder_code,
        code=submission.source.source,
        language_key=submission.language.key,
    )

    if not sub_res.get("success"):
        submission.status = 'IE'
        submission.result = 'IE'
        submission.error = f"Lỗi nộp bài sang NTUCoder: {sub_res.get('error', 'Không xác định')}"
        submission.save(update_fields=['status', 'result', 'error'])
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    ntucoder_sub_id = sub_res["submission_id"]
    submission.ntucoder_submission_id = ntucoder_sub_id
    submission.save(update_fields=['ntucoder_submission_id'])
    logger.info(f"Submission {submission.id} submitted to NTUCoder as #{ntucoder_sub_id}")

    # 4. Poll for verdict with timeout
    max_attempts = 45  # 45 * 2s = 90s
    poll_result = None

    for attempt in range(max_attempts):
        time.sleep(2)
        poll = poll_ntucoder_submission(ntucoder_sub_id)
        if poll.get("is_final"):
            poll_result = poll
            break

    if not poll_result:
        submission.status = 'IE'
        submission.result = 'IE'
        submission.error = "Hết thời gian chờ kết quả chấm từ NTUCoder."
        submission.save(update_fields=['status', 'result', 'error'])
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    # 5. Record result and test cases
    verdict = poll_result["verdict"]
    submission.status = 'CE' if verdict == 'CE' else 'D'
    submission.result = verdict
    submission.time = poll_result.get("time_seconds", 0.0)
    submission.memory = poll_result.get("memory_kb", 0)
    submission.points = poll_result.get("points", 0.0)
    submission.error = poll_result.get("verdict_text", "")
    submission.judged_date = timezone.now()

    # Create synthetic testcase entries
    submission.test_cases.all().delete()
    failed_case = poll_result.get("failed_case")
    cases_to_create = []
    total_cases = max(10, (failed_case or 10))

    if verdict == 'AC':
        for i in range(1, total_cases + 1):
            cases_to_create.append(SubmissionTestCase(
                submission=submission,
                case=i,
                status='AC',
                time=submission.time / total_cases if submission.time else 0.0,
                memory=submission.memory or 0,
                points=100.0 / total_cases,
                total=100.0 / total_cases,
            ))
        submission.case_points = 100.0
        submission.case_total = 100.0
    else:
        last_case = failed_case if failed_case else 1
        for i in range(1, last_case):
            cases_to_create.append(SubmissionTestCase(
                submission=submission,
                case=i,
                status='AC',
                time=0.01,
                memory=submission.memory or 0,
                points=10.0,
                total=10.0,
            ))
        cases_to_create.append(SubmissionTestCase(
            submission=submission,
            case=last_case,
            status=verdict,
            time=submission.time or 0.0,
            memory=submission.memory or 0,
            points=0.0,
            total=10.0,
        ))
        submission.case_points = 0.0
        submission.case_total = float(total_cases * 10)

    SubmissionTestCase.objects.bulk_create(cases_to_create)
    submission.save(update_fields=['status', 'result', 'time', 'memory', 'points', 'error', 'judged_date', 'case_points', 'case_total'])

    # 6. Update contest rankings & user profile points
    submission.refresh_from_db()
    submission.update_contest()
    profile.calculate_points()

    # 7. Broadcast completed websocket event
    event.post(f'sub_{submission.id_secret}', {
        'type': 'grading-end',
        'result': verdict,
        'points': submission.points,
        'time': submission.time,
        'memory': submission.memory,
    })
    _post_update_submission(submission, done=True)

    logger.info(f"Graded NTUCoder submission {submission.id} (#{ntucoder_sub_id}): {verdict} ({submission.points} pts)")
    return True


def judge_ntucoder_submission_async(submission: Submission):
    """
    Queue asynchronous judging for NTUCoder submission.
    """
    submission.status = 'QU'
    submission.save(update_fields=['status'])
    return judge_ntucoder_submission_task.delay(submission.id)
