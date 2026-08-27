from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .errors import DomainError
from .services import AGENT_STATUSES, AgentChatRoomService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PresenceSession:
    project_id: str
    session_id: str
    token: str
    status: str = "idle"


class LocalPresenceManager:
    """Keep sessions owned by one long-running local MCP process alive."""

    def __init__(
        self,
        room_service: AgentChatRoomService,
        *,
        enabled: bool,
        interval_seconds: float,
    ) -> None:
        self.room_service = room_service
        self.enabled = enabled
        self.interval_seconds = interval_seconds
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

    def register(
        self,
        project_id: str,
        session_id: str,
        token: str,
        *,
        status: str = "idle",
    ) -> None:
        if not self.enabled:
            return
        normalized_status = self._validate_status(status)
        with self._lock:
            self._sessions[session_id] = PresenceSession(
                project_id=project_id,
                session_id=session_id,
                token=token,
                status=normalized_status,
            )

    def ensure_registered(
        self,
        project_id: str,
        session_id: str,
        token: str,
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
        with self._lock:
            if session_id in self._sessions:
                return False
            self._sessions[session_id] = PresenceSession(
                project_id=project_id,
                session_id=session_id,
                token=token,
            )
            return True

    def set_status(self, session_id: str, status: str) -> None:
        if not self.enabled:
            return
        normalized_status = self._validate_status(status)
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None:
                self._sessions[session_id] = PresenceSession(
                    project_id=current.project_id,
                    session_id=current.session_id,
                    token=current.token,
                    status=normalized_status,
                )

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def heartbeat_once(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                self.room_service.heartbeat(
                    session.project_id,
                    session.session_id,
                    session.token,
                    status=session.status,
                )
            except DomainError as error:
                if error.code in {"session_not_found", "invalid_session_token"}:
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

    @staticmethod
    def _validate_status(status: str) -> str:
        normalized = status.strip().lower()
        if normalized not in AGENT_STATUSES - {"offline"}:
            raise ValueError(f"Unsupported presence status: {status}")
        return normalized
