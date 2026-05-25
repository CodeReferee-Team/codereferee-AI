from __future__ import annotations

import argparse
import time

from app.workflow.repository_validation import process_next_repository_validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CodeReferee repository-validation Redis worker.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
    parser.add_argument("--idle-sleep", type=float, default=2.0, help="Seconds to sleep when the queue is empty.")
    args = parser.parse_args()

    while True:
        state = process_next_repository_validation()
        if state is None:
            if args.once:
                print("No queued repository validation job.")
                return
            time.sleep(args.idle_sleep)
            continue

        print(f"Processed repository validation job {state.job_id}: {state.status}")
        if args.once:
            return


if __name__ == "__main__":
    main()
