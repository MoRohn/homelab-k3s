#!/usr/bin/env python3
"""Block private material from entering this PUBLIC repo. Never prints a secret value.

  tools/check-private.py            check staged files (what the pre-commit hook runs)
  tools/check-private.py --all      check every tracked file (run before a push or a history rewrite)

Three checks:
  1. path:    nothing under secrets/, personal/, private/ (except their README.md), no .env*,
              keys, certs, kubeconfigs, or local AI-assistant files
  2. value:   no exact copy of a credential currently stored in secrets/ or .env.local
  3. pattern: no private-key blocks or well-known token formats
"""
import pathlib, re, subprocess, sys

ROOT = pathlib.Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())

BLOCKED_PATH = re.compile(r"""^(
      (secrets|personal|private)/(?!README\.md$).+     # the three private roots
    | (.*/)?\.env(\..*)?$ | .*\.env$                     # dotenv files
    | .*\.(key|pem|p12|crt|kubeconfig)$ | (.*/)?kubeconfig.* | (.*/)?k3s\.yaml$
    | (.*/)?CLAUDE\.local\.md$ | (.*/)?\.claude/(?!settings\.json$).+
)$""", re.X)
ALLOWED_PATH = re.compile(r".*\.env\.example$")

TOKEN_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Tailscale key", re.compile(r"tskey-[a-z]+-[A-Za-z0-9]{10,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    # AKIAIOSFODNN7EXAMPLE is the key in AWS's own docs (SigV4 test vectors), never a real one
    ("AWS access key", re.compile(r"\bAKIA(?!IOSFODNN7EXAMPLE\b)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{20,}")),
]
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|PASS$", re.I)


def local_secret_values():
    """Credential values on this machine. Whole-file secrets (*.key, *.token) count in full;
    dotenv files contribute only variables whose NAME says secret (usernames and endpoints
    legitimately appear in tracked docs)."""
    vals = set()
    files = [p for p in (ROOT / "secrets").glob("*") if p.is_file() and p.name != "README.md"]
    files.append(ROOT / ".env.local")
    for p in files:
        if not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        if p.suffix in (".key", ".token"):
            vals.update(l.strip() for l in text.splitlines() if len(l.strip()) >= 12)
            continue
        for line in text.splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if m and SECRET_NAME.search(m.group(1)):
                v = m.group(2).strip().strip("'\"")
                if len(v) >= 12:
                    vals.add(v)
    return vals


def git_lines(*args):
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [l for l in out.split("\0") if l]


def main():
    all_mode = "--all" in sys.argv
    if all_mode:
        paths = git_lines("ls-files", "-z")
        read = lambda p: (ROOT / p).read_bytes() if (ROOT / p).is_file() else b""
    else:
        paths = git_lines("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
        read = lambda p: subprocess.run(["git", "show", f":{p}"], cwd=ROOT, capture_output=True).stdout

    secrets = local_secret_values()
    problems = []
    for p in paths:
        if BLOCKED_PATH.match(p) and not ALLOWED_PATH.match(p):
            problems.append((p, "private path (see .gitignore)"))
            continue
        data = read(p)
        if b"\0" in data[:8000]:          # binary: path rules only
            continue
        text = data.decode("utf-8", errors="ignore")
        if any(v in text for v in secrets):
            problems.append((p, "contains a credential stored in secrets/ or .env.local"))
        for name, rx in TOKEN_PATTERNS:
            if rx.search(text):
                problems.append((p, f"looks like a {name}"))

    if problems:
        print("✋ check-private: refusing — this repo is PUBLIC.\n", file=sys.stderr)
        for p, why in problems:
            print(f"   {p}: {why}", file=sys.stderr)
        print("\n   Move it to secrets/, personal/ or private/ (all git-ignored), or unstage it:"
              "\n     git restore --staged <file>"
              "\n   False positive? Fix the rule in tools/check-private.py rather than using --no-verify.",
              file=sys.stderr)
        return 1
    print(f"check-private: {len(paths)} file(s) clean ({'tracked' if all_mode else 'staged'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
