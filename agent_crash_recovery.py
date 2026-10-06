"""Restart policy for an agent CLI that exited: resume its session or start fresh.

The wrapper relaunches the CLI whenever it exits. Before this policy every
relaunch was a new, empty session, so a crash lost everything the agent had in
its head, even though the transcript was still on disk. Now:

  - deliberate exit (0 e.g. /exit, 130 Ctrl-C, 143 SIGTERM) -> fresh session,
    no notice;
  - crash (any other code, or none recorded: killed with its tmux session)
    -> resume the last session (claude --resume <id>, codex resume <id>),
    unless it is unknown, or this launch was itself a resume that crashed
    within QUICK_RECRASH_SECONDS (the session may be what crashes the CLI)
    -> fresh session.

After a crash the agent is nudged to look at its channel and continue, unless
it crashed CRASH_LOOP_COUNT times within CRASH_WINDOW_SECONDS; then it is not
nudged (so it can't keep re-running whatever crashes it) and relaunches back off.
"""

import time
from dataclasses import dataclass

RESUMABLE_PROVIDERS = ("claude", "codex")
DELIBERATE_EXIT_CODES = (0, 130, 143)
QUICK_RECRASH_SECONDS = 120
CRASH_WINDOW_SECONDS = 15 * 60
CRASH_LOOP_COUNT = 3
RESTART_DELAY_SECONDS = 3
CRASH_LOOP_DELAY_SECONDS = 30


@dataclass
class RestartDecision:
    exit_code: int | None
    crashed: bool
    resume_session_id: str  # "" = fresh session
    nudge: bool
    delay: float

    @property
    def resumed(self) -> bool:
        return bool(self.resume_session_id)


def resume_args(provider: str, base_args: list[str], session_id: str) -> list[str]:
    """Launch arguments that reopen session_id instead of starting a new one."""
    if provider == "claude":
        return [*base_args, "--resume", session_id]
    if provider == "codex":
        return ["resume", session_id, *base_args]
    return list(base_args)


def nudge_prompt(channel: str, decision: RestartDecision) -> str:
    """Prompt typed into the relaunched CLI so it picks its work back up."""
    if decision.resumed:
        state = "Your CLI crashed and was restarted with your previous session resumed."
    else:
        state = ("Your CLI crashed and was restarted in a fresh session (the old one "
                 "could not be resumed); the channel summary at the top of your read "
                 "has the history.")
    return (f"mcp read #{channel} - {state} Check the channel and continue any task "
            "you were in the middle of. If what you were doing may have caused the crash "
            "(e.g. a memory-heavy local job), do not repeat it; say so in chat instead.")


class CrashRecoveryPolicy:
    def __init__(self, provider: str, session_id_fn, *, now_fn=time.time):
        self.provider = provider
        self.session_id_fn = session_id_fn  # -> current session id ("" if unknown)
        self.now_fn = now_fn
        self._crash_times: list[float] = []
        self._next_resume_id = ""
        self._launched_resume_id = ""

    def launch_args(self, base_args: list[str]) -> list[str]:
        """Arguments for the launch about to happen (consumes a pending resume)."""
        session_id, self._next_resume_id = self._next_resume_id, ""
        self._launched_resume_id = session_id
        return resume_args(self.provider, base_args, session_id) if session_id else list(base_args)

    def on_exit(self, exit_code: int | None, uptime: float) -> RestartDecision:
        """Decide how to relaunch after the CLI exited with exit_code after uptime seconds."""
        now = self.now_fn()
        crashed = exit_code not in DELIBERATE_EXIT_CODES
        if crashed:
            self._crash_times = [t for t in self._crash_times if now - t < CRASH_WINDOW_SECONDS]
            self._crash_times.append(now)
        crash_loop = len(self._crash_times) >= CRASH_LOOP_COUNT

        session_id = ""
        # Judged by uptime alone: a resume may continue under a new session id.
        failed_resume = bool(self._launched_resume_id) and uptime < QUICK_RECRASH_SECONDS
        if crashed and self.provider in RESUMABLE_PROVIDERS and not failed_resume:
            session_id = self.session_id_fn() or ""
        self._next_resume_id = session_id

        return RestartDecision(
            exit_code=exit_code,
            crashed=crashed,
            resume_session_id=session_id,
            nudge=crashed and not crash_loop,
            delay=CRASH_LOOP_DELAY_SECONDS if crashed and crash_loop else RESTART_DELAY_SECONDS,
        )
