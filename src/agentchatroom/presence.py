from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from .errors import DomainError
from .services import AgentChatRoomService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PresenceSession:
    project_id: str
    session_id: str
    token: str
    agent_key: str
    transport_key: str = ""


class LocalPresenceManager:
    """Keep sessions owned by one long-running local MCP process alive."""

    def __init__(
        self,
        room_service: AgentChatRoomService,
        *,
        enabled: bool,
        interval_seconds: float,
        availability_check: Callable[[], None] | None = None,
        transport_check: Callable[[str], bool] | None = None,
    ) -> None:
        self.room_service = room_service
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.availability_check = availability_check
        self.transport_check = transport_check
        self._sessions: dict[str, PresenceSession] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="agentchatroom-presence",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        self._thread = None
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                self.room_service.leave_session(
                    session.project_id, session.session_id, session.token
                )
            except DomainError:
                pass

    def register(
        self,
        project_id: str,
        session_id: str,
        token: str,
        *,
        agent_key: str,
        transport_key: str = "",
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._sessions[session_id] = PresenceSession(
                project_id=project_id,
                session_id=session_id,
                token=token,
                agent_key=agent_key,
                transport_key=transport_key,
            )

    def ensure_registered(
        self,
        project_id: str,
        session_id: str,
        token: str,
        *,
        agent_key: str = "",
        transport_key: str = "",
    ) -> bool:
        """Register a live session for background heartbeat if missing.

        The presence registry is process-local memory: an MCP process restart
        empties it even though the Session row (and its client-held token)
        stays valid. Any successful authenticated tool call proves the session
        is still owned by this process, so use it to resume keepalive.

        Returns True when a new registration was added.
        """
        if not self.enabled:
            return False
        resolved_agent_key = agent_key.strip()
        if not resolved_agent_key:
            try:
                sessions = self.room_service.snapshot(project_id)["agents"]
            except DomainError:
                return False
            session = next(
                (item for item in sessions if str(item["id"]) == session_id),
                None,
            )
            if session is None:
                return False
            resolved_agent_key = str(session.get("agent_key") or session_id)
        with self._lock:
            if session_id in self._sessions:
                return False
            self._sessions[session_id] = PresenceSession(
                project_id=project_id,
                session_id=session_id,
                token=token,
                agent_key=resolved_agent_key,
                transport_key=transport_key,
            )
            return True

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def heartbeat_once(self) -> None:
        if self.availability_check is not None:
            try:
                self.availability_check()
            except DomainError:
                with self._lock:
                    self._sessions.clear()
                return
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if (
                self.transport_check is not None
                and session.transport_key
                and not self.transport_check(session.transport_key)
            ):
                # The transport is gone (idle reap, server restart, client
                # reload). Stop the keepalive so the session cannot stay online
                # forever, but keep the Room Session itself: presence is derived
                # from the last proof of life, so it is reported offline once the
                # window lapses, while a client that returns on the same
                # credential is adopted at the transport layer and must still be
                # able to submit. Explicit leave (session_leave, server shutdown,
                # token revocation) remains the only event that closes a session
                # and releases its leases.
                self.unregister(session.session_id)
                logger.info(
                    "Transport for session %s is gone; keepalive stopped",
                    session.session_id,
                )
                continue
            try:
                self.room_service.heartbeat(
                    session.project_id,
                    session.session_id,
                    session.token,
                )
            except DomainError as error:
                if error.code in {
                    "session_not_found",
                    "invalid_session_token",
                    "session_closed",
                }:
                    self.unregister(session.session_id)
                logger.warning(
                    "Presence heartbeat failed for session %s: %s",
                    session.session_id,
                    error.code,
                )
            except Exception:
                logger.exception(
                    "Presence heartbeat failed for session %s",
                    session.session_id,
                )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.heartbeat_once()
