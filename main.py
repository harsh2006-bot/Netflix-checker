"""
main.py — Entry point for justrunmy.app / any Python host.

Upload the ZIP of this project to justrunmy.app and set the run command to:
    python main.py

What this does automatically:
  1. Upgrades pip
  2. Installs every package with smart version fallbacks
     (if one version fails → tries older versions → skips only if ALL fail)
  3. Starts the Telegram bot
"""

import subprocess
import sys
import os

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(args, show=False):
    """Run a command; return True on success."""
    if show:
        ret = subprocess.call(args)
    else:
        ret = subprocess.call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ret == 0


def _pip(*specs, extra_flags=None, show=False):
    """
    Try each pip spec in order.  Return the spec that succeeded, or None.
    Example: _pip("flask>=2.3", "flask>=2.0", "flask")
    """
    flags = extra_flags or []
    for spec in specs:
        print(f"  pip install {spec} ...", end=" ", flush=True)
        cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + flags + [spec]
        ok = _run(cmd, show=False)
        if ok:
            print("OK")
            return spec
        print("FAILED — trying next fallback")
    # Last resort: print the error visibly
    print(f"  [WARN] All fallbacks failed for {specs[0]!r}. Showing pip output:")
    subprocess.call([sys.executable, "-m", "pip", "install"] + flags + [specs[-1]])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Upgrade pip
# ─────────────────────────────────────────────────────────────────────────────
print("=" * 60)
print(" Netflix Checker Bot — Auto-Bootstrap")
print("=" * 60)
print("[1/3] Upgrading pip ...")
_run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
print("      pip upgrade done.")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Install all packages with fallback chains
# ─────────────────────────────────────────────────────────────────────────────
print("[2/3] Installing packages ...")

# greenlet MUST be installed first with --only-binary so it never tries to
# compile from source (compilation is killed by the OOM killer on low-memory
# hosts like justrunmy.app which run Python 3.12+).
print("  Installing greenlet (binary only) ...")
_pip("greenlet>=3.0.3", "greenlet>=2.0.0", "greenlet",
     extra_flags=["--only-binary", ":all:"])

PACKAGES = [
    # (primary, fallback1, fallback2, ...)  — first one that works wins
    ("requests>=2.28.0",       "requests"),
    ("pyTelegramBotAPI>=4.14", "pyTelegramBotAPI>=4.0","pyTelegramBotAPI"),
    ("flask>=2.0.0",           "flask"),
    ("colorama>=0.4.0",        "colorama"),
    ("urllib3>=1.26.0",        "urllib3"),
    # supabase has breaking API changes across major versions — try newest first
    ("supabase>=2.0.0",        "supabase==1.2.0",      "supabase==1.0.3", "supabase"),
]

for specs in PACKAGES:
    _pip(*specs)

print("      Package installs complete.")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Start the bot
# ─────────────────────────────────────────────────────────────────────────────
print("[3/3] Starting bot ...")
os.environ["_BOOTSTRAP_DONE"] = "1"

# Import then explicitly call main() so the bot actually starts.
# (The guard `if __name__ == "__main__"` in netflix_checker.py won't fire
#  when the module is imported, so we call main() ourselves.)
try:
    import netflix_checker
    netflix_checker.main()
except Exception as _e:
    print(f"[ERROR] Bot crashed on start: {_e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)


