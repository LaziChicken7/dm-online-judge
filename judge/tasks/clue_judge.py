import logging
import time
import threading
import re
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger('judge.clue_judge')

@shared_task(name='judge.tasks.submission.judge_clue_submission_task')
def judge_clue_submission_task(submission_id):
    from judge.models import Submission, SubmissionTestCase, Problem
    from judge.utils.clue_service import (
        submit_clue_solution,
        poll_clue_submission,
        map_lang_to_clue,
        get_profile_clue_opener,
    )
    from judge import event_poster as event

    try:
        submission = Submission.objects.select_related('problem', 'user').get(id=submission_id)
    except Submission.DoesNotExist:
        logger.error(f"Submission #{submission_id} does not exist.")
        return False

    problem = submission.problem
    profile = submission.user
    source = submission.source.source if hasattr(submission, 'source') and submission.source else ''
    lang_key = submission.language.key if submission.language else 'CPP17'
    clue_lang_id = map_lang_to_clue(lang_key)

    # 1. Update state to Grading / In progress
    Submission.objects.filter(id=submission_id).update(status='P', result=None)
    event.post(f'sub_{submission.id_secret}', {'type': 'grading'})

    # 2. Check user's ClueOJ credentials
    clue_code = problem.clue_code or problem.code
    logger.info(f"Submitting submission #{submission_id} to ClueOJ problem {clue_code} (lang: {clue_lang_id})...")

    # If submitter doesn't have an active clue session, try to find any working clue profile
    opener, _ = get_profile_clue_opener(profile)
    active_profile = profile
    if not opener:
        # Check if there is an account with clue_username configured (e.g. LaziChicken7)
        from judge.models import Profile
        fallback_profile = Profile.objects.filter(clue_username__gt='').first()
        if fallback_profile:
            active_profile = fallback_profile

    sub_res = submit_clue_solution(active_profile, clue_code, clue_lang_id, source)
    if not sub_res.get('success'):
        err_msg = sub_res.get('error', 'Lỗi không xác định khi nộp bài lên ClueOJ.')
        logger.warning(f"ClueOJ submit failed for #{submission_id}: {err_msg}")
        Submission.objects.filter(id=submission_id).update(
            status='D',
            result='CE',
            error=err_msg,
            points=0.0,
            case_points=0.0,
            case_total=1.0,
            judged_date=timezone.now(),
        )
        SubmissionTestCase.objects.create(
            submission=submission,
            case=1,
            status='CE',
            points=0.0,
            total=1.0,
            time=0.0,
            memory=0,
            feedback=err_msg,
        )
        submission.refresh_from_db()
        submission.update_contest()
        profile.calculate_points()
        event.post(f'sub_{submission.id_secret}', {
            'type': 'done',
            'result': 'CE',
            'points': 0.0,
            'case_points': 0.0,
            'case_total': 1.0,
            'time': 0.0,
            'memory': 0,
            'status_text': err_msg,
        })
        return False

    remote_sub_id = sub_res['submission_id']
    Submission.objects.filter(id=submission_id).update(clue_submission_id=remote_sub_id)
    logger.info(f"Submission #{submission_id} submitted to ClueOJ as #{remote_sub_id}. Polling verdict...")

    # 3. Poll ClueOJ until grading finishes (max 90 seconds)
    max_attempts = 35
    poll_data = None
    for attempt in range(max_attempts):
        time.sleep(2.5)
        poll_data = poll_clue_submission(active_profile, remote_sub_id)
        if poll_data.get('done'):
            logger.info(f"ClueOJ #{remote_sub_id} grading completed on attempt #{attempt + 1}: {poll_data.get('verdict')}")
            break

    if not poll_data:
        poll_data = {'done': True, 'verdict': 'IE', 'error': 'Hết thời gian chờ kết quả từ ClueOJ.'}

    verdict = poll_data.get('verdict') or 'IE'
    cases = poll_data.get('cases', [])
    cases_count = len(cases)
    earned_pts = float(poll_data.get('points', 0.0))
    max_pts = float(poll_data.get('max_points', 0.0))
    time_sec = float(poll_data.get('time', 0.0))
    memory_kb = int(poll_data.get('memory', 0))
    feedback = poll_data.get('feedback', '')

    # Calculate final DMOJ points
    if max_pts > 0:
        scaled_points = round((earned_pts / max_pts) * problem.points, 1)
    elif verdict == 'AC':
        scaled_points = float(problem.points)
    else:
        scaled_points = 0.0

    if not problem.partial and scaled_points < problem.points:
        scaled_points = 0.0

    # Save test cases
    SubmissionTestCase.objects.filter(submission_id=submission_id).delete()
    if cases:
        case_objs = []
        for c in cases:
            case_objs.append(SubmissionTestCase(
                submission=submission,
                case=c['case'],
                status=c['status'],
                points=c.get('points', 1.0 if c['status'] == 'AC' else 0.0),
                total=c.get('total', 1.0),
                time=c['time'],
                memory=c['memory'],
                feedback=''
            ))
        SubmissionTestCase.objects.bulk_create(case_objs)
        case_points = float(sum(c['points'] for c in case_objs))
        case_total = float(sum(c['total'] for c in case_objs))
    else:
        case_points = 1.0 if verdict == 'AC' else 0.0
        case_total = 1.0
        SubmissionTestCase.objects.create(
            submission=submission,
            case=1,
            status=verdict,
            points=case_points,
            total=1.0,
            time=time_sec,
            memory=memory_kb,
            feedback=feedback or verdict
        )

    # 4. Save final result in DMOJ database
    Submission.objects.filter(id=submission_id).update(
        status='D',
        result=verdict,
        points=scaled_points,
        case_points=case_points,
        case_total=case_total,
        time=time_sec,
        memory=memory_kb,
        error=feedback,
        judged_date=timezone.now(),
    )
    submission.refresh_from_db()
    submission.update_contest()
    profile.calculate_points()

    # 5. Broadcast live websocket event
    event.post(f'sub_{submission.id_secret}', {
        'type': 'done',
        'result': verdict,
        'points': scaled_points,
        'case_points': case_points,
        'case_total': case_total,
        'time': time_sec,
        'memory': memory_kb,
        'status_text': verdict,
    })
    return True


def judge_clue_submission_async(submission):
    """
    Dispatch ClueOJ judging task asynchronously via Celery or background thread.
    """
    sub_id = submission.id
    try:
        judge_clue_submission_task.delay(sub_id)
        logger.info(f"Dispatched Celery task for ClueOJ submission #{sub_id}")
    except Exception as e:
        logger.warning(f"Failed to dispatch Celery task ({e}), falling back to background thread")
        t = threading.Thread(target=judge_clue_submission_task, args=(sub_id,), daemon=True)
        t.start()
