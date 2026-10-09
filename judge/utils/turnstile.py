# -*- coding: utf-8 -*-
import json
import logging
import urllib.parse
import urllib.request
from django.conf import settings

logger = logging.getLogger('judge.turnstile')

DEFAULT_TURNSTILE_SITE_KEY = '1x00000000000000000000AA'
DEFAULT_TURNSTILE_SECRET_KEY = '1x0000000000000000000000000000000AA'

def get_turnstile_site_key():
    return getattr(settings, 'CLOUDFLARE_TURNSTILE_SITE_KEY', DEFAULT_TURNSTILE_SITE_KEY)

def get_turnstile_secret_key():
    return getattr(settings, 'CLOUDFLARE_TURNSTILE_SECRET_KEY', DEFAULT_TURNSTILE_SECRET_KEY)

def is_turnstile_enabled():
    return bool(get_turnstile_site_key() and get_turnstile_secret_key())

def verify_turnstile(token, remoteip=None):
    if not is_turnstile_enabled():
        return True
    if not token:
        return False
    secret_key = get_turnstile_secret_key()
    data = {'secret': secret_key, 'response': token}
    if remoteip:
        data['remoteip'] = remoteip
    req = urllib.request.Request(
        'https://challenges.cloudflare.com/turnstile/v0/siteverify',
        data=urllib.parse.urlencode(data).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            success = bool(res.get('success', False))
            if not success:
                logger.warning('Turnstile verification failed: %s', res.get('error-codes', []))
            return success
    except Exception as e:
        logger.warning('Turnstile verification network error: %s', e)
        return False
