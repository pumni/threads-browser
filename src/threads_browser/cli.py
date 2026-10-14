"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from .storage import (
    AccountStore,
    BrowserDataError,
    CheckpointStore,
    profile_lock,
    resolve_data_root,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads-browser")
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("account", help="register an existing profile")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    add = account_commands.add_parser("add", help="register an existing profile reference")
    add.add_argument("alias")
    add.add_argument("--actor", required=True, help="expected visible Threads actor")
    add.add_argument("--profile-dir", required=True, type=Path)
    nurture = commands.add_parser("nurture", help="run a bounded read-only workflow")
    nurture_commands = nurture.add_subparsers(dest="nurture_command", required=True)
    run = nurture_commands.add_parser("run", help="sample Home / For You once")
    run.add_argument("alias")
    run.add_argument("--headed", action="store_true", help="confirm headed one-shot operation")
    run.add_argument("--max-posts", required=True, type=int)
    run.add_argument(
        "--term",
        action="append",
        required=True,
        help="local relevance term; repeat for more terms",
    )
    return parser


def _format_result(result: object) -> str:
    ratio_value = result.home_relevant_ratio
    ratio = "n/a" if ratio_value is None else f"{ratio_value:.2f}"
    return (
        f"NURTURE status={result.status} "
        "source=HOME_FOR_YOU "
        f"ui={result.ui_status} "
        f"home_sampled={result.home_sampled} "
        f"home_relevant={result.home_relevant} "
        f"home_irrelevant={result.home_irrelevant} "
        f"home_unknown={result.home_unknown} "
        f"home_relevant_ratio={ratio} "
        f"duplicates={result.duplicates} "
        f"detail_reads={result.detail_reads} "
        f"scrolls={result.scrolls} "
        f"checkpoint_runs={result.checkpoint_runs}"
    )

def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        data_root = resolve_data_root()
        accounts = AccountStore(data_root)
        if args.command == "account" and args.account_command == "add":
            accounts.add(args.alias, args.actor, args.profile_dir)
            print("ACCOUNT status=REGISTERED")
            return 0
        if args.command == "nurture" and args.nurture_command == "run":
            if not args.headed:
                print("ERROR HEADED_REQUIRED", file=sys.stderr)
                return 1
            if not 1 <= args.max_posts <= 20:
                print("ERROR POST_LIMIT_INVALID", file=sys.stderr)
                return 1
            account = accounts.get(args.alias)
            lock = profile_lock(data_root, account)
            with lock:
                from .reader import UiReadError
                from .workflow import run_browser_nurture

                try:
                    result = asyncio.run(
                        run_browser_nurture(
                            account,
                            CheckpointStore(data_root),
                            max_posts=args.max_posts,
                            terms=tuple(args.term),
                        )
                    )
                except UiReadError as error:
                    print(f"ERROR {error.code}", file=sys.stderr)
                    return 1
                print(_format_result(result))
                return 0
    except BrowserDataError as error:
        print(f"ERROR {error.code}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR INTERRUPTED", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
