from django.core.cache import cache


def finished_submission(sub):
    uid = getattr(sub, 'user_id', None)
    if not uid and hasattr(sub, 'user') and sub.user:
        uid = sub.user.id
    if not uid:
        return
    keys = ['user_complete:%d' % uid, 'user_attempted:%s' % uid]
    if hasattr(sub, 'contest') and sub.contest:
        try:
            participation = sub.contest.participation
            keys += ['contest_complete:%d' % participation.id]
            keys += ['contest_attempted:%d' % participation.id]
        except Exception:
            pass
    cache.delete_many(keys)
