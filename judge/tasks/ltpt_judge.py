import logging
import time
from celery import shared_task
from django.utils import timezone
from judge import event_poster as event
from judge.models import Submission, SubmissionTestCase
from judge.judgeapi import _post_update_submission
from judge.utils.ltpt_service import check_ltpt_login, login_ltpt, poll_ltpt_submission, submit_ltpt_solution

logger = logging.getLogger('judge.ltpt_judge')


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def judge_ltpt_submission_task(self, submission_id: int):
    try:
        submission = Submission.objects.select_related('problem', 'user').get(id=submission_id)
    except Submission.DoesNotExist:
        logger.error(f"Submission {submission_id} does not exist for LTPT judge.")
        return False

    problem = submission.problem
    profile = submission.user

    if not problem.is_ltpt:
        logger.error(f"Problem {problem.code} is not an LTPT problem.")
        return False

    # 1. Notify grading begin
    submission.status = 'QU'
    submission.save(update_fields=['status'])
    event.post(f'sub_{submission.id_secret}', {'type': 'grading-begin'})
    _post_update_submission(submission)

    # 2. Check & ensure LTPT login session
    cookie = profile.ltpt_cookie
    is_valid = False
    if cookie:
        check = check_ltpt_login(cookie)
        is_valid = check.get("logged_in", False)

    if not is_valid and profile.ltpt_username and profile.ltpt_password:
        res = login_ltpt(profile.ltpt_username, profile.ltpt_password)
        if res.get("success"):
            cookie = res["cookie"]
            profile.ltpt_cookie = cookie
            profile.save(update_fields=["ltpt_cookie"])
            is_valid = True

    if not is_valid or not cookie:
        submission.status = 'IE'
        submission.result = 'IE'
        submission.error = "Chưa kết nối hoặc phiên đăng nhập LapTrinhPhoThong đã hết hạn. Vui lòng kết nối lại tài khoản LTPT của bạn."
        submission.save(update_fields=['status', 'result', 'error'])
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    # 3. Submit solution to LTPT
    sub_res = submit_ltpt_solution(
        cookie=cookie,
        problem_code=problem.ltpt_code,
        code=submission.source.source,
        language_key=submission.language.key,
    )

    if not sub_res.get("success"):
        submission.status = 'IE'
        submission.result = 'IE'
        submission.error = f"Lỗi nộp bài sang LTPT: {sub_res.get('error', 'Không xác định')}"
        submission.save(update_fields=['status', 'result', 'error'])
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    ltpt_sub_id = sub_res["submission_id"]
    submission.ltpt_submission_id = ltpt_sub_id
    submission.save(update_fields=['ltpt_submission_id'])
    logger.info(f"Submission {submission.id} submitted to LTPT as #{ltpt_sub_id}")

    # 4. Poll for verdict with timeout
    max_attempts = 45  # 45 * 2s = 90s
    poll_result = None

    for attempt in range(max_attempts):
        time.sleep(2)
        poll = poll_ltpt_submission(cookie, ltpt_sub_id)
        if poll.get("is_final"):
            poll_result = poll
            break

    if not poll_result:
        submission.status = 'IE'
        submission.result = 'IE'
        submission.error = "Hết thời gian chờ kết quả chấm từ LapTrinhPhoThong."
        submission.save(update_fields=['status', 'result', 'error'])
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {'type': 'internal-error'})
        _post_update_submission(submission, done=True)
        return False

    # 5. Record result and detailed test cases
    verdict = poll_result["verdict"]
    submission.status = 'CE' if verdict == 'CE' else 'D'
    submission.result = verdict
    submission.time = poll_result.get("time_seconds", 0.0)
    submission.memory = poll_result.get("memory_kb", 0)
    submission.points = poll_result.get("points", 0.0)
    submission.error = poll_result.get("error", "")
    submission.judged_date = timezone.now()

    submission.test_cases.all().delete()
    cases_data = poll_result.get("cases", [])
    if cases_data:
        cases_to_create = []
        total_case_points = 0.0
        total_case_max = 0.0
        total_raw = sum(c.get("total", 1.0) for c in cases_data) or 1.0
        pts_scale = (problem.points or 100.0) / total_raw
        for c in cases_data:
            c_pt = c.get("points", 0.0) * pts_scale
            c_tot = c.get("total", 1.0) * pts_scale
            cases_to_create.append(SubmissionTestCase(
                submission=submission,
                case=c["case"],
                status=c["status"],
                time=c["time"],
                memory=c["memory"],
                points=c_pt,
                total=c_tot,
            ))
            total_case_points += c_pt
            total_case_max += c_tot
        SubmissionTestCase.objects.bulk_create(cases_to_create)
        submission.case_points = total_case_points
        submission.case_total = total_case_max
    else:
        # Fallback synthetic case
        SubmissionTestCase.objects.create(
            submission=submission,
            case=1,
            status=verdict,
            time=submission.time or 0.0,
            memory=submission.memory or 0,
            points=submission.points or 0.0,
            total=100.0,
        )
        submission.case_points = submission.points or 0.0
        submission.case_total = 100.0

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

    logger.info(f"Graded LTPT submission {submission.id} (#{ltpt_sub_id}): {verdict} ({submission.points} pts)")
    return True


def judge_ltpt_submission_async(submission: Submission):
    """
    Queue asynchronous judging for LTPT submission.
    """
    submission.status = 'QU'
    submission.save(update_fields=['status'])
    return judge_ltpt_submission_task.delay(submission.id)
