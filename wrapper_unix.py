"""Mac/Linux agent injection — uses tmux send-keys to type into the agent CLI.

Called by wrapper.py on Mac and Linux. Requires tmux to be installed.
  - Mac:   brew install tmux
  - Linux: apt install tmux  (or yum, pacman, etc.)

How it works:
  1. Creates a tmux session running the agent CLI
  2. Queue watcher sends keystrokes via 'tmux send-keys'
  3. Wrapper attaches to the session so you see the full TUI
  4. Ctrl+B, D to detach (agent keeps running in background)
"""

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _session_exists(session_name: str) -> bool:
    """Return True while the tmux session is still alive."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def _check_tmux():
    """Verify tmux is installed, exit with helpful message if not."""
    if shutil.which("tmux"):
        return
    print("\n  Error: tmux is required for auto-trigger on Mac/Linux.")
    if sys.platform == "darwin":
        print("  Install: brew install tmux")
    else:
        print("  Install: apt install tmux  (or yum/pacman equivalent)")
    sys.exit(1)


def _pane_content(tmux_session: str) -> str:
    """Capture current tmux pane text."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_session, "-p"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _cli_is_ready(tmux_session: str) -> bool:
    """Check if the CLI inside the tmux pane is ready for input.

    Looks for common prompt indicators from supported CLIs
    (Claude ❯, Codex ›, Gemini ❯/$, generic $) in recent lines.
    """
    content = _pane_content(tmux_session)
    if not content.strip():
        return False
    # Check last few visible lines for a prompt character
    for line in reversed(content.strip().splitlines()[-8:]):
        stripped = line.strip()
        if not stripped:
            continue
        # Prompt chars at start of line indicate ready state
        if stripped[0] in ("❯", "›", ">", "$", "%"):
            return True
    return False


def inject(text: str, *, tmux_session: str):
    """Send text + Enter to a tmux session via send-keys.

    Waits for the CLI to show its prompt before sending, then verifies
    Enter was processed — retries if the text is still in the input area.
    """
    # Wait for CLI to be ready (up to 30s for cold start / model loading)
    for _ in range(60):
        if _cli_is_ready(tmux_session):
            break
        time.sleep(0.5)

    # Use -l to send text literally (avoids misinterpreting as key names)
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "-l", text],
        capture_output=True,
    )

    # Let TUI render the text before sending Enter
    time.sleep(0.5)
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "Enter"],
        capture_output=True,
    )

    # Verify Enter was accepted — if injected text is still sitting on a
    # prompt line, the CLI likely swallowed Enter during init; retry.
    snippet = text[:50]
    for _attempt in range(5):
        time.sleep(1.0)
        content = _pane_content(tmux_session)
        if not content:
            break
        # Check last few lines for prompt + our text (still in input box)
        still_pending = False
        for line in reversed(content.strip().splitlines()[-6:]):
            if snippet in line and any(c in line for c in "❯›>$%"):
                still_pending = True
                break
        if not still_pending:
            break
        # Retry Enter
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_session, "Enter"],
            capture_output=True,
        )


_RESUME_DIALOG_OPTIONS = ("Resume from summary", "Resume full session as-is", "Don't ask me again")


def accept_resume_dialog(tmux_session: str, timeout: float = 20.0, still_current=None) -> bool:
    """Answer Claude Code's "Resume from summary / Resume full session" dialog,
    shown when resuming a large session whose prompt cache went cold, with its
    default (resume from summary).

    Watches the whole timeout (the dialog can appear after the input box has
    rendered), and only the bottom of the pane with all three options on it,
    so text about the dialog in the resumed conversation does not match.
    Returns True if the dialog was answered; False after the timeout, or as
    soon as still_current() says this launch has been superseded."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if still_current is not None and not still_current():
            return False
        bottom = "\n".join(_pane_content(tmux_session).splitlines()[-15:])
        if all(option in bottom for option in _RESUME_DIALOG_OPTIONS):
            subprocess.run(["tmux", "send-keys", "-t", tmux_session, "Enter"],
                           capture_output=True)
            return True
        time.sleep(0.5)
    return False


def get_activity_checker(session_name, trigger_flag=None):
    """Return a callable that detects tmux pane output by hashing content."""
    last_hash = [None]

    def check():
        # External trigger: queue watcher injected a message
        if trigger_flag is not None and trigger_flag[0]:
            trigger_flag[0] = False
            return True
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, timeout=2,
            )
            h = hash(result.stdout)
            changed = last_hash[0] is not None and h != last_hash[0]
            last_hash[0] = h
            return changed
        except Exception:
            return False

    return check


def build_agent_command(command, args, strip_env=None, inject_env=None,
                        exit_code_file=None) -> str:
    """Shell command that tmux runs for the agent CLI."""
    agent_cmd = " ".join([shlex.quote(command)] + [shlex.quote(a) for a in args])

    # Build env(1) prefix for the command INSIDE the tmux session.
    # subprocess.run(env=...) only affects the tmux client binary — the
    # session shell inherits from the tmux server instead.  Use env(1)
    # to set (-u to unset, VAR=val to inject) vars in the actual session.
    env_parts = []
    if strip_env:
        env_parts.extend(f"-u {shlex.quote(v)}" for v in strip_env)
    if inject_env:
        env_parts.extend(
            f"{shlex.quote(k)}={shlex.quote(v)}"
            for k, v in inject_env.items()
        )
    if env_parts:
        agent_cmd = f"env {' '.join(env_parts)} {agent_cmd}"

    if exit_code_file:
        # Record the CLI's exit code (the crash-recovery policy needs it). Run
        # under sh so `$?` works whatever shell tmux is configured with.
        record = f"{agent_cmd}; echo $? > {shlex.quote(str(exit_code_file))}"
        agent_cmd = f"sh -c {shlex.quote(record)}"
    return agent_cmd


def read_exit_code(exit_code_file) -> int | None:
    """The recorded exit code, or None (killed with the session, or not recorded)."""
    if not exit_code_file:
        return None
    try:
        return int(Path(exit_code_file).read_text("utf-8").strip())
    except (OSError, ValueError):
        return None


def run_agent(
    command,
    extra_args,
    cwd,
    env,
    queue_file,
    agent,
    no_restart,
    start_watcher,
    strip_env=None,
    pid_holder=None,
    session_name=None,
    inject_env=None,
    detached=False,
    launch_args_fn=None,
    on_exit_fn=None,
    exit_code_file=None,
):
    """Run agent inside a tmux session, inject via tmux send-keys.

    launch_args_fn(extra_args) -> args for each (re)launch (e.g. resume a session);
    on_exit_fn(exit_code, uptime_seconds) -> seconds to wait before relaunching.
    """
    _check_tmux()

    session_name = session_name or f"agentchattr-{agent}"

    def _restart_delay(started: float) -> float:
        exit_code = read_exit_code(exit_code_file)
        code_text = f" (code {exit_code})" if exit_code is not None else ""
        print(f"\n  {agent.capitalize()} exited{code_text}.")
        if on_exit_fn is None:
            return 3
        try:
            return on_exit_fn(exit_code, time.time() - started)
        except Exception as exc:
            print(f"  Restart policy failed ({exc}); restarting fresh.")
            return 3

    # Resolve cwd to absolute path (tmux -c needs it)
    abs_cwd = str(Path(cwd).resolve())

    # Wire up injection with the tmux session name
    inject_fn = lambda text: inject(text, tmux_session=session_name)
    start_watcher(inject_fn)

    print(f"  Using tmux session: {session_name}")
    print(f"  Detach: Ctrl+B, D  (agent keeps running)")
    print(f"  Reattach: tmux attach -t {session_name}\n")

    while True:
        try:
            # Clean up stale session from a previous crash
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )

            args = launch_args_fn(extra_args) if launch_args_fn else extra_args
            agent_cmd = build_agent_command(command, args, strip_env, inject_env, exit_code_file)
            if exit_code_file:
                Path(exit_code_file).unlink(missing_ok=True)
            started = time.time()

            # Create tmux session running the agent CLI
            result = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", abs_cwd, agent_cmd],
                env=env,
            )
            if result.returncode != 0:
                print(f"  Error: failed to create tmux session (exit {result.returncode})")
                break

            if detached:
                print(f"  Detached startup complete.")
                print(f"  Reattach: tmux attach -t {session_name}")
                while _session_exists(session_name):
                    time.sleep(1)
                if no_restart:
                    break
                delay = _restart_delay(started)
                print(f"  Restarting in {delay:g}s... (Ctrl+C to quit)")
                time.sleep(delay)
                continue

            # Attach — blocks until agent exits or user detaches (Ctrl+B, D)
            subprocess.run(["tmux", "attach-session", "-t", session_name])

            # Check: did the agent exit, or did the user just detach?
            if _session_exists(session_name):
                # Session still alive — user detached, agent running in background.
                # Keep the wrapper alive so the local proxy and heartbeats survive.
                print(f"\n  Detached. {agent.capitalize()} still running in tmux.")
                print(f"  Reattach: tmux attach -t {session_name}")
                while _session_exists(session_name):
                    time.sleep(1)
                break

            # Session gone — agent exited
            if no_restart:
                break

            delay = _restart_delay(started)
            print(f"  Restarting in {delay:g}s... (Ctrl+C to quit)")
            time.sleep(delay)
        except KeyboardInterrupt:
            # Kill the tmux session on Ctrl+C
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            break
