"""Short command line entry point for the local Browser services."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from .account_service import add_local_account, open_account_login
from .login import LoginStatus
from .storage import (
    AccountStore,
    BrowserDataError,
    CheckpointStore,
    TermsStore,
    profile_lock,
    resolve_data_root,
)


def _add_legacy_registration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", help="expected visible Threads actor for an existing profile")
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument(
        "--create-profile",
        action="store_true",
        help="create the absent dedicated Chromium profile directory",
    )


def _add_run_options(parser: argparse.ArgumentParser, *, legacy: bool) -> None:
    parser.add_argument(
        "--headed",
        action="store_true",
        help="accepted for compatibility; Chromium is always visible",
    )
    parser.add_argument(
        "--show-post-text",
        action="store_true",
        help="print verified extracted post text to an interactive terminal",
    )
    parser.add_argument(
        "--show-observed-text",
        action="store_true",
        help="print identity-unverified observed text to an interactive terminal",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="print an operational trace and extracted post text to an interactive terminal",
    )
    parser.add_argument(
        "--max-posts",
        required=legacy,
        type=int,
        default=None if legacy else 20,
        help="maximum visible post rows per viewport (1-20; default: 20)",
    )
    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=8,
        help="maximum observed read-only feed advances (0-20; default: 8)",
    )
    parser.add_argument(
        "--max-total-posts",
        type=int,
        default=40,
        help="maximum distinct verified posts per run (1-400; default: 40)",
    )
    parser.add_argument(
        "-t",
        "--term",
        action="append",
        dest="terms",
        required=legacy,
        help="one-time local relevance term; repeat for more terms",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tb")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="create a local account")
    add.add_argument("alias")
    _add_legacy_registration_options(add)

    login = commands.add_parser("login", help="open an account's persistent Chromium profile")
    login.add_argument("alias")

    commands.add_parser("ls", help="list local account aliases and verification status")

    terms = commands.add_parser("terms", help="view or replace an account's nurture terms")
    terms.add_argument("alias")
    terms.add_argument("terms", nargs="*", help="replacement relevance terms")
    terms.add_argument("--clear", action="store_true", help="clear saved relevance terms")

    run = commands.add_parser("run", help="run bounded, read-only nurture")
    run.add_argument("alias")
    _add_run_options(run, legacy=False)

    account = commands.add_parser("account", help="legacy account management commands")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    legacy_add = account_commands.add_parser(
        "add", help="create a local account or register a dedicated profile"
    )
    legacy_add.add_argument("alias")
    _add_legacy_registration_options(legacy_add)
    legacy_login = account_commands.add_parser(
        "login", help="open the account's persistent Chromium profile"
    )
    legacy_login.add_argument("alias")
    legacy_login.add_argument(
        "--headed",
        action="store_true",
        help="accepted for compatibility; login is always visible",
    )

    nurture = commands.add_parser("nurture", help="legacy bounded nurture command")
    nurture_commands = nurture.add_subparsers(dest="nurture_command", required=True)
    legacy_run = nurture_commands.add_parser(
        "run", help="search Home / For You within bounded budgets"
    )
    legacy_run.add_argument("alias")
    _add_run_options(legacy_run, legacy=True)
    return parser


def _format_result(result: object) -> str:
    ratio_value = result.home_relevant_ratio
    ratio = "n/a" if ratio_value is None else f"{ratio_value:.2f}"
    return (
        f"NURTURE status={result.status} "
        "source=HOME_FOR_YOU "
        f"stop_reason={getattr(result, 'stop_reason', 'UNKNOWN')} "
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
        f"viewport_row_appearances={getattr(result, 'viewport_observations', getattr(result, 'observed_rows', 0))} "
        f"body_extracted={getattr(result, 'body_extracted', 0)} "
        f"provenance_verified={getattr(result, 'provenance_verified', 0)} "
        f"unique_provenance_identities={getattr(result, 'unique_provenance_identities', getattr(result, 'provenance_verified', 0))} "
        f"unique_verified_posts={getattr(result, 'unique_verified_posts', 0)} "
        f"unique_deferred_posts={getattr(result, 'unique_deferred_posts', 0)} "
        f"unresolved_gaps={getattr(result, 'unresolved_gaps', 0)} "
        f"visible_text_segments={getattr(result, 'visible_text_segments', 0)} "
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


def _print_login_status(status: LoginStatus) -> None:
    if status == "WAITING_FOR_AUTHENTICATION":
        print("LOGIN status=WAITING_FOR_AUTHENTICATION")
        print(
            "Log in or complete verification in Chromium. "
            "Close the browser or press Ctrl+C to cancel."
        )
    elif status == "SESSION_REUSED":
        print("LOGIN status=SESSION_REUSED")
    elif status == "VERIFIED":
        print("LOGIN status=VERIFIED")
    elif status == "BROWSER_OPEN":
        print("BROWSER status=OPEN")


async def _prompt_readiness_extension(stage: str, retry: int) -> bool:
    while True:
        try:
            answer = await asyncio.to_thread(
                input,
                f"Stage {stage} is still waiting (retry={retry}). "
                "Enter to wait again, q to stop safely: ",
            )
        except EOFError:
            return False
        if not answer.strip():
            return True
        if answer.strip().casefold() in {"q", "quit"}:
            return False
        print("Enter to wait again, q to stop safely.")


def _run_diagnostics_check(args: argparse.Namespace) -> bool:
    if not (args.detail or args.show_post_text or args.show_observed_text):
        return True
    if not _has_interactive_terminal():
        error = (
            "DETAIL_TTY_REQUIRED"
            if args.detail
            else "OBSERVED_TEXT_TTY_REQUIRED"
            if args.show_observed_text
            else "POST_TEXT_TTY_REQUIRED"
        )
        print(f"ERROR {error}", file=sys.stderr)
        return False
    if args.detail:
        print(
            "WARNING DETAIL_LOCAL_ONLY extracted post text may remain in terminal "
            "scrollback; do not capture or share this output in GitHub, PRs, chats, or CI logs.",
            file=sys.stderr,
        )
        return True
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
    return True


def _is_login(args: argparse.Namespace) -> bool:
    return args.command == "login" or (
        args.command == "account" and args.account_command == "login"
    )


def _is_add(args: argparse.Namespace) -> bool:
    return args.command == "add" or (
        args.command == "account" and args.account_command == "add"
    )


def _is_run(args: argparse.Namespace) -> bool:
    return args.command == "run" or (
        args.command == "nurture" and args.nurture_command == "run"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if _is_run(args) and not _run_diagnostics_check(args):
        return 1

    try:
        data_root = resolve_data_root()
        accounts = AccountStore(data_root)
        if _is_add(args):
            if args.actor is None and args.profile_dir is None:
                add_local_account(args.alias, data_root=data_root)
            else:
                accounts.add(
                    args.alias,
                    args.actor,
                    args.profile_dir,
                    create_profile=args.create_profile,
                )
            print("ACCOUNT status=REGISTERED")
            return 0

        if _is_login(args):
            from .reader import UiReadError

            try:
                result = asyncio.run(
                    open_account_login(
                        args.alias,
                        data_root=data_root,
                        status_callback=_print_login_status,
                    )
                )
            except UiReadError as error:
                print(f"STOP {error.code}", file=sys.stderr)
                return 1
            if result.status == "CANCELLED":
                print("LOGIN status=CANCELLED")
                return 1
            return 0

        if args.command == "ls":
            listed_accounts = accounts.list()
            if not listed_accounts:
                print("No local accounts.")
            for account in listed_accounts:
                verification = "VERIFIED" if account.expected_actor is not None else "UNVERIFIED"
                print(f"{account.alias} {verification}")
            return 0

        if args.command == "terms":
            account = accounts.get(args.alias)
            terms_store = TermsStore(data_root)
            if args.clear and args.terms:
                print("ERROR TERMS_ARGUMENTS_INVALID", file=sys.stderr)
                return 1
            if args.clear:
                terms_store.clear(account.alias)
                print("TERMS status=CLEARED")
            elif args.terms:
                try:
                    configured = terms_store.set(account.alias, tuple(args.terms))
                except BrowserDataError as error:
                    if error.code != "TERMS_INVALID":
                        raise
                    print("ERROR TERMS_INVALID", file=sys.stderr)
                    return 1
                print(f"TERMS status=SAVED count={len(configured)}")
            else:
                configured = terms_store.get(account.alias)
                if not configured:
                    print("TERMS status=NOT_CONFIGURED")
                else:
                    print("\n".join(configured))
            return 0

        if _is_run(args):
            legacy = args.command == "nurture"
            if legacy and not args.headed:
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
            if account.expected_actor is None:
                print("ERROR ACCOUNT_NOT_VERIFIED", file=sys.stderr)
                return 1
            if args.terms:
                terms = tuple(args.terms)
            else:
                terms = TermsStore(data_root).get(account.alias)
            if not terms:
                print("ERROR TERMS_NOT_CONFIGURED", file=sys.stderr)
                print(
                    f'Configure terms with: tb terms {account.alias} "hiring"',
                    file=sys.stderr,
                )
                return 1
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
                            terms=terms,
                            max_scrolls=args.max_scrolls,
                            max_total_posts=args.max_total_posts,
                            show_post_text=args.show_post_text,
                            show_observed_text=args.show_observed_text,
                            detail=args.detail,
                            readiness_prompt=(
                                _prompt_readiness_extension
                                if _has_interactive_terminal()
                                else None
                            ),
                        )
                    )
                except UiReadError as error:
                    if args.detail:
                        print("[CHECKPOINT] status=NOT_SAVED", file=sys.stderr)
                        print(f"[STOP] code={error.code}", file=sys.stderr)
                    else:
                        print(f"STOP {error.code}", file=sys.stderr)
                    if error.code == "LOGIN_REQUIRED":
                        print(f"Run 'tb login {account.alias}' to authenticate.", file=sys.stderr)
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
