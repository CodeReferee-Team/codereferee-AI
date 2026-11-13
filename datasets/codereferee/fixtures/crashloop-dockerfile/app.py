import os
import sys

# BUG: a required env var is read unguarded at startup. It is not provided,
# so the process crashes on boot -> Kubernetes CrashLoopBackOff, rollout never
# becomes ready.
DATABASE_URL = os.environ["DATABASE_URL"]

print("starting with", DATABASE_URL)
sys.exit(0)
