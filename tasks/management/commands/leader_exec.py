"""
Run a child command only while holding a Redis leader lease.

PLS-207: the pulse Deployment runs >1 replica, and supervisord starts
celery-beat and the recurring-timer scheduler in EVERY replica. Both must be
singletons: duplicate beat double-enqueues every periodic sweep, and a second
scheduler's startup bootstrap re-adds recurring timers mid-fire, double-
advancing due_date (skipped recurring cycles).

This wrapper contends for `pulse:leader:<name>` (SET NX EX). The winner execs
the child and renews the lease; losers poll until the lease frees (leader pod
died/rolled) and then take over. If renewal is lost (Redis flush, lease stolen),
the child is terminated and the process exits non-zero — supervisord's
autorestart re-enters contention. Net effect: exactly one live instance
cluster-wide, with automatic failover, no chart/topology change.

Usage (supervisord.conf):
    python manage.py leader_exec --name beat -- celery -A taskmanager beat ...
    python manage.py leader_exec --name scheduler -- python manage.py run_scheduler
"""

import os
import signal
import socket
import subprocess
import sys
import time

import redis
from django.conf import settings
from django.core.management.base import BaseCommand

LEASE_TTL = 60          # seconds a lease lives without renewal
RENEW_EVERY = 15        # renewal cadence (well under TTL)
CONTEND_EVERY = 10      # how often a standby retries acquisition

RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class Command(BaseCommand):
    help = 'Exec a child command only while holding a Redis leader lease'

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True,
                            help='lease name (pulse:leader:<name>)')
        parser.add_argument('cmd', nargs='+',
                            help='child command (prefix with -- to stop option parsing)')

    def handle(self, *args, **options):
        name = options['name']
        cmd = options['cmd']
        key = f'pulse:leader:{name}'
        me = f'{socket.gethostname()}:{os.getpid()}'
        r = redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
        renew = r.register_script(RENEW_LUA)

        self.stdout.write(f'[leader_exec:{name}] contending for {key} as {me}')
        while True:
            try:
                if r.set(key, me, nx=True, ex=LEASE_TTL):
                    break
                holder = r.get(key)
            except redis.RedisError as e:
                self.stderr.write(f'[leader_exec:{name}] redis error while contending: {e}')
                time.sleep(CONTEND_EVERY)
                continue
            self.stdout.write(f'[leader_exec:{name}] standby (leader={holder})')
            time.sleep(CONTEND_EVERY)

        self.stdout.write(f'[leader_exec:{name}] acquired lease — starting: {" ".join(cmd)}')
        child = subprocess.Popen(cmd)

        def _forward(signum, frame):
            child.send_signal(signum)
        signal.signal(signal.SIGTERM, _forward)
        signal.signal(signal.SIGINT, _forward)

        exit_code = 1
        try:
            while True:
                rc = child.poll()
                if rc is not None:
                    exit_code = rc
                    self.stdout.write(f'[leader_exec:{name}] child exited rc={rc}')
                    break
                time.sleep(RENEW_EVERY)
                try:
                    ok = renew(keys=[key], args=[me, LEASE_TTL])
                except redis.RedisError as e:
                    self.stderr.write(f'[leader_exec:{name}] redis error on renew: {e} — retaining child, retrying')
                    continue
                if not ok:
                    self.stderr.write(f'[leader_exec:{name}] LOST lease — terminating child')
                    child.terminate()
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    exit_code = 1
                    break
        finally:
            try:
                # Release only if still ours, so a new leader isn't evicted.
                if r.get(key) == me:
                    r.delete(key)
            except redis.RedisError:
                pass
        sys.exit(exit_code)
