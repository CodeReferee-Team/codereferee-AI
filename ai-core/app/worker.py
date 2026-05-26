from __future__ import annotations

import argparse

from app.workflow.repository_validation import process_next_repository_validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CodeReferee repository-validation Redis worker.")
    parser.add_argument("--once", action="store_true", help="Process one queued job and exit.")
    parser.add_argument(
        "--block-timeout",
        type=int,
        default=0,
        help="BLPOP timeout in seconds. 0 waits forever until a task arrives.",
    )
    args = parser.parse_args()

    while True:
        state = process_next_repository_validation(block=True, timeout=args.block_timeout)
        if state is None:
            if args.once:
                print("No queued repository validation job before BLPOP timeout.")
                return
            continue

        print(f"Processed repository validation job {state.job_id}: {state.status}")
        if args.once:
            return


if __name__ == "__main__":
    main()
