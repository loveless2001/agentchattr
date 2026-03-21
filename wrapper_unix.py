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
import threading
import time


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


_TRUST_PATTERNS = [
    "Do you trust the contents of this directory",  # codex
    "Do you want to proceed?",                      # claude MCP approval
    "Press enter to continue",                      # codex trust
]


def _auto_approve_trust_prompts(session_name: str, timeout: int = 60):
    """Watch a tmux pane for trust/approval prompts and auto-send Enter.

    Runs for up to `timeout` seconds after session creation, covering the
    startup window where these prompts appear.
    """
    deadline = time.time() + timeout
    approved = set()
    while time.time() < deadline:
        if not _session_exists(session_name):
            return
        content = _pane_content(session_name)
        for pattern in _TRUST_PATTERNS:
            if pattern in content and pattern not in approved:
                print(f"  [auto-approve] Detected trust prompt in {session_name}: '{pattern}'")
                time.sleep(0.5)
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "Enter"],
                    capture_output=True,
                )
                print(f"  [auto-approve] Sent Enter to {session_name}")
                approved.add(pattern)
                time.sleep(1)
                break
        time.sleep(0.5)
    if approved:
        print(f"  [auto-approve] Done for {session_name}: approved {len(approved)} prompt(s)")
    else:
        print(f"  [auto-approve] No trust prompts detected for {session_name} (timed out after {timeout}s)")


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
):
    """Run agent inside a tmux session, inject via tmux send-keys."""
    _check_tmux()

    session_name = session_name or f"agentchattr-{agent}"
    agent_cmd = " ".join(
        [shlex.quote(command)] + [shlex.quote(a) for a in extra_args]
    )

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

    # Resolve cwd to absolute path (tmux -c needs it)
    from pathlib import Path
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

            # Create tmux session running the agent CLI
            result = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", abs_cwd, agent_cmd],
                env=env,
            )
            if result.returncode != 0:
                print(f"  Error: failed to create tmux session (exit {result.returncode})")
                break

            # Auto-approve trust/permission prompts during startup
            threading.Thread(
                target=_auto_approve_trust_prompts,
                args=(session_name,),
                daemon=True,
            ).start()

            if detached:
                print(f"  Detached startup complete.")
                print(f"  Reattach: tmux attach -t {session_name}")
                while _session_exists(session_name):
                    time.sleep(1)
                if no_restart:
                    break
                print(f"\n  {agent.capitalize()} exited.")
                print(f"  Restarting in 3s... (Ctrl+C to quit)")
                time.sleep(3)
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

            print(f"\n  {agent.capitalize()} exited.")
            print(f"  Restarting in 3s... (Ctrl+C to quit)")
            time.sleep(3)
        except KeyboardInterrupt:
            # Kill the tmux session on Ctrl+C
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            break
