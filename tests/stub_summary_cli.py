"""Stand-in for the summary CLI (`codex exec`) in tests: like the real one it
reads the prompt on stdin and prints one line, short and deterministic.
Set AGENTCHATTR_TEST_LUNA=1 to run the end-to-end test with the real CLI."""

import hashlib
import sys

prompt = sys.stdin.read()
print(f"stub-summary {hashlib.sha1(prompt.encode()).hexdigest()[:10]}")
