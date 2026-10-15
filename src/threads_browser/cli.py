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
    account = commands.add_parser("account", help="register and maintain account profiles")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    add = account_commands.add_parser("add", help="register a dedicated profile reference")
    add.add_argument("alias")
    add.add_argument("--actor", required=True, help="expected visible Threads actor")
    add.add_argument("--profile-dir", required=True, type=Path)
    add.add_argument(
        "--create-profile",
        action="store_true",
        help="create the absent dedicated Chromium profile directory",
    )
    login = account_commands.add_parser("login", help="log in manually in headed Chromium")
    login.add_argument("alias")
    login.add_argument("--headed", action="store_true", help="confirm visible browser operation")
    nurture = commands.add_parser("nurture", help="run a bounded read-only workflow")
    nurture_commands = nurture.add_subparsers(dest="nurture_command", required=True)
    run = nurture_commands.add_parser("run", help="search Home / For You within bounded budgets")
    run.add_argument("alias")
    run.add_argument("--headed", action="store_true", help="confirm headed one-shot operation")
    run.add_argument(
        "--show-post-text",
        action="store_true",
        help="print verified extracted post text to an interactive terminal",
    )
    run.add_argument(
        "--show-observed-text",
        action="store_true",
        help="print identity-unverified observed text to an interactive terminal",
    )
    run.add_argument(
        "--max-posts",
        required=True,
        type=int,
        help="maximum visible post rows to account for per viewport (1-20)",
    )
    run.add_argument(
        "--max-scrolls",
        type=int,
        default=8,
        help="maximum observed read-only feed advances (0-20; default: 8)",
    )
    run.add_argument(
        "--max-total-posts",
        type=int,
        default=40,
        help="maximum distinct verified posts per run (1-400; default: 40)",
    )
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
        f"home_new={result.home_new} "
        f"home_relevant={result.home_relevant} "
        f"home_irrelevant={result.home_irrelevant} "
        f"home_unknown={result.home_unknown} "
        f"home_relevant_ratio={ratio} "
        f"duplicates={result.duplicates} "
        f"detail_reads={result.detail_reads} "
        f"scroll_attempts={result.scroll_attempts} "
        f"scrolls={result.scrolls} "
        f"skipped_unverified={result.skipped_unverified} "
        f"checkpoint_runs={result.checkpoint_runs} "
        f"observed_rows={getattr(result, 'observed_rows', 0)} "
        f"body_extracted={getattr(result, 'body_extracted', 0)} "
        f"provenance_verified={getattr(result, 'provenance_verified', 0)} "
        f"classified={getattr(result, 'classified', 0)} "
        f"observation_unknown={getattr(result, 'observation_unknown', 0)} "
        f"unverified_relevant={getattr(result, 'unverified_relevant', 0)} "
        f"skipped_rows={getattr(result, 'skipped_rows', 0)} "
        "viewport_coverage_complete="
        f"{str(getattr(result, 'viewport_coverage_complete', True)).lower()}"
    )


def _has_interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):
        return False


async def _prompt_readiness_extension(stage: str, retry: int) -> bool:
    while True:
        try:
            answer = await asyncio.to_thread(
                input,
                f"Stage {stage} is still waiting (retry={retry}). "
                "Enter to wait again, q to stop safely: "
            )
        except EOFError:
            return False
        if not answer.strip():
            return True
        if answer.strip().casefold() in {"q", "quit"}:
            return False
        print("Enter to wait again, q to stop safely.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "account" and args.account_command == "login":
        if not args.headed:
            print("ERROR HEADED_REQUIRED", file=sys.stderr)
            return 1
        if not _has_interactive_terminal():
            print("ERROR LOGIN_TTY_REQUIRED", file=sys.stderr)
            return 1
    if (
        args.command == "nurture"
        and args.nurture_command == "run"
        and (args.show_post_text or args.show_observed_text)
    ):
        if not _has_interactive_terminal():
            error = (
                "OBSERVED_TEXT_TTY_REQUIRED"
                if args.show_observed_text
                else "POST_TEXT_TTY_REQUIRED"
            )
            print(f"ERROR {error}", file=sys.stderr)
            return 1
        if args.show_post_text:
            print(
                "WARNING POST_TEXT_LOCAL_ONLY terminal scrollback may retain content; "
                "do not paste preview text into PRs or chats.",
                file=sys.stderr,
            )
        if args.show_observed_text:
            print(
                "WARNING OBSERVED_TEXT_LOCAL_ONLY identity is unverified and terminal "
                "scrollback may retain content; do not paste preview text into PRs or chats.",
                file=sys.stderr,
            )
    try:
        data_root = resolve_data_root()
        accounts = AccountStore(data_root)
        if args.command == "account" and args.account_command == "add":
            accounts.add(
                args.alias,
                args.actor,
                args.profile_dir,
                create_profile=args.create_profile,
            )
            print("ACCOUNT status=REGISTERED")
            return 0
        if args.command == "account" and args.account_command == "login":
            account = accounts.get(args.alias)
            with profile_lock(data_root, account):
                from .login import run_manual_login
                from .reader import UiReadError

                try:
                    result = asyncio.run(run_manual_login(account))
                except UiReadError as error:
                    print(f"STOP {error.code}", file=sys.stderr)
                    return 1
                if result.status == "VERIFIED":
                    print("LOGIN status=VERIFIED")
                    return 0
                if result.status == "CANCELLED":
                    print("LOGIN status=CANCELLED")
                    return 1
                print(f"STOP {result.error_code or 'LOGIN_REQUIRED'}", file=sys.stderr)
                return 1
        if args.command == "nurture" and args.nurture_command == "run":
            if not args.headed:
                print("ERROR HEADED_REQUIRED", file=sys.stderr)
                return 1
            if not 1 <= args.max_posts <= 20:
                print("ERROR POST_LIMIT_INVALID", file=sys.stderr)
                return 1
            if not 0 <= args.max_scrolls <= 20:
                print("ERROR SCROLL_LIMIT_INVALID", file=sys.stderr)
                return 1
            if not 1 <= args.max_total_posts <= 400:
                print("ERROR TOTAL_SCAN_LIMIT_INVALID", file=sys.stderr)
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
                            max_scrolls=args.max_scrolls,
                            max_total_posts=args.max_total_posts,
                            show_post_text=args.show_post_text,
                            show_observed_text=args.show_observed_text,
                            readiness_prompt=(
                                _prompt_readiness_extension
                                if _has_interactive_terminal()
                                else None
                            ),
                        )
                    )
                except UiReadError as error:
                    print(f"STOP {error.code}", file=sys.stderr)
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
