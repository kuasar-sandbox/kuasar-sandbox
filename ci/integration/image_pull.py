"""Bounded retries for immutable execution-image transport only."""
import random
import re
import subprocess
import sys
import time


def pull_failure(error):
    """Return retry permission and a safe reason code, never raw registry text."""
    diagnostic = error.stderr or ''
    if isinstance(diagnostic, bytes):
        diagnostic = diagnostic.decode('utf-8', errors='replace')
    diagnostic = diagnostic.lower()
    permanent = {
        'auth': ('unauthorized', 'authentication required', 'invalid credentials',
                 'denied', 'forbidden', 'status code: 401', 'status code: 403'),
        'cert': ('certificate', 'x509'),
        'notfound': ('not found', 'manifest unknown', 'name unknown', 'status code: 404'),
        'digest': ('digest mismatch', 'digest verification', 'invalid checksum',
                   'unexpected digest', 'content digest'),
        'platform': ('no matching manifest', 'unsupported platform'),
        'invalid-reference': ('invalid reference',),
    }
    for reason, messages in permanent.items():
        if any(message in diagnostic for message in messages):
            return False, reason
    if isinstance(error, subprocess.TimeoutExpired):
        return True, 'timeout'
    # Go's HTTP client also reports a plain terminal EOF after a quoted URL.
    # Match the error token, not an arbitrary "eof" substring in a URL/image.
    if re.search(r'(?:^|:\s*)eof\s*$', diagnostic, re.MULTILINE):
        return True, 'eof'
    transient = ('i/o timeout', 'connection reset', 'connection refused',
                 'connection timed out', 'tls handshake timeout', 'unexpected eof',
                 'temporary failure in name resolution', 'temporary failure',
                 'context deadline exceeded', 'request canceled while waiting',
                 'network is unreachable', 'net/http: request canceled',
                 'too many requests', 'toomanyrequests', 'service unavailable',
                 'bad gateway', 'gateway timeout')
    if (any(message in diagnostic for message in transient)
            or re.search(r'\b(?:status(?: code)?|http)[: ]+(?:429|500|502|503|504)\b', diagnostic)):
        return True, 'transport'
    return False, 'unknown'


def pull_retryable(error):
    return pull_failure(error)[0]


def pull_image(reference):
    # One acquisition budget, including sleeps; no retry of build/E2E assertions.
    deadline = time.monotonic() + 30 * 60
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(['docker', 'pull', reference], 30 * 60)
        try:
            subprocess.run(['docker', 'pull', reference], check=True,
                           timeout=min(15 * 60, remaining),
                           stderr=subprocess.PIPE, text=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # Registry errors can contain signed URLs; report status, not raw stderr.
            retryable, reason = pull_failure(error)
            status = 'timeout' if isinstance(error, subprocess.TimeoutExpired) else f'exit {error.returncode}'
            print(f'CI image pull: attempt {attempt + 1}/3 failed ({status}; reason={reason})', file=sys.stderr)
            if attempt == 2 or not retryable:
                raise
            remaining = deadline - time.monotonic()
            delay = random.uniform(0.8, 1.2) * (30 * 2 ** attempt)
            if remaining <= delay:
                raise
            print(f'CI image pull: transient transport failure; retry {attempt + 2}/3 '
                  f'in {delay:.1f}s', file=sys.stderr)
            time.sleep(delay)
        else:
            return
