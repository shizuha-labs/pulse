"""Hermetic-test socket guard used by PLAT-1358 templates.

Install this only in test settings/fixtures. It blocks accidental outbound
network connects while still allowing loopback/UNIX sockets for ephemeral,
test-owned services such as a local Postgres or Redis sidecar.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Iterable


class HermeticityError(RuntimeError):
    """Raised when a test attempts a real outbound network connection."""


def _csv_env(name: str) -> set[str]:
    return {part.strip().lower() for part in os.environ.get(name, '').split(',') if part.strip()}


def _is_loopback_host(host: object) -> bool:
    if host in (None, ''):
        return True
    if not isinstance(host, str):
        return False
    lowered = host.strip().lower().rstrip('.')
    if lowered in {'localhost', 'ip6-localhost'}:
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


@dataclass
class SocketGuard:
    allowed_hosts: set[str]
    allow_loopback: bool = True

    def __post_init__(self) -> None:
        self._orig_socket_connect = socket.socket.connect
        self._orig_create_connection = socket.create_connection
        self._installed = False

    def _allowed(self, address: object) -> bool:
        if not isinstance(address, tuple) or not address:
            # UNIX sockets / non-IP address families are local test-owned resources.
            return True
        host = address[0]
        if isinstance(host, bytes):
            host = host.decode('utf-8', 'ignore')
        host_s = str(host).strip().lower().rstrip('.')
        if host_s in self.allowed_hosts:
            return True
        return self.allow_loopback and _is_loopback_host(host_s)

    def _raise(self, address: object) -> None:
        raise HermeticityError(
            'Outbound network connection blocked by PLAT-1358 hermetic test guard: '
            f'{address!r}. Inject a fake/stub, short-circuit under settings.TESTING, '
            'or add a loopback test-owned dependency.'
        )

    def install(self) -> 'SocketGuard':
        if self._installed:
            return self

        guard = self

        def guarded_socket_connect(sock: socket.socket, address: object):  # type: ignore[no-untyped-def]
            if not guard._allowed(address):
                guard._raise(address)
            return guard._orig_socket_connect(sock, address)  # type: ignore[arg-type]

        def guarded_create_connection(address: object, *args, **kwargs):  # type: ignore[no-untyped-def]
            if not guard._allowed(address):
                guard._raise(address)
            return guard._orig_create_connection(address, *args, **kwargs)  # type: ignore[arg-type]

        socket.socket.connect = guarded_socket_connect  # type: ignore[method-assign]
        socket.create_connection = guarded_create_connection  # type: ignore[assignment]
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        socket.socket.connect = self._orig_socket_connect  # type: ignore[method-assign]
        socket.create_connection = self._orig_create_connection  # type: ignore[assignment]
        self._installed = False


@contextlib.contextmanager
def socket_guard(allowed_hosts: Iterable[str] | None = None, *, allow_loopback: bool = True):
    merged = set(allowed_hosts or set()) | _csv_env('SHIZUHA_HERMETIC_ALLOWED_HOSTS')
    guard = SocketGuard({h.strip().lower().rstrip('.') for h in merged if h.strip()}, allow_loopback=allow_loopback).install()
    try:
        yield guard
    finally:
        guard.uninstall()
