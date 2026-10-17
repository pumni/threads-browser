"""Short command line entry point for the local Browser services."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from .account_service import add_local_account, open_account_login
from .cli_arguments import _parser
from .cli_results import _format_result
from .cli_status import (
    _finish_scheduled_status,
    _mark_scheduled_failure,
    _mark_scheduled_result,
    _new_scheduled_status,
    _record_known_checkpoint_runs,
    _safe_status_code,
)
from .console_output import ConsoleOutputPolicy
from .login import LoginStatus
from .storage import (
    AccountStore,
    BrowserAccount,
    BrowserDataError,
    CheckpointStore,
    TermsStore,
    resolve_data_root,
    write_operation_status,
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
    detail = getattr(args, "detail", False)
    show_post_text = getattr(args, "show_post_text", False)
    show_observed_text = getattr(args, "show_observed_text", False)
    if not (detail or show_post_text or show_observed_text):
        return True
    if not _has_interactive_terminal():
        error = (
            "DETAIL_TTY_REQUIRED"
            if detail
            else "OBSERVED_TEXT_TTY_REQUIRED"
            if show_observed_text
            else "POST_TEXT_TTY_REQUIRED"
        )
        print(f"ERROR {error}", file=sys.stderr)
        return False
    if detail:
        print(
            "WARNING DETAIL_LOCAL_ONLY extracted post text may remain in terminal "
            "scrollback; do not capture or share this output in GitHub, PRs, chats, or CI logs.",
            file=sys.stderr,
        )
        return True
    if show_post_text:
        print(
            "WARNING POST_TEXT_LOCAL_ONLY terminal scrollback may retain content; "
            "do not paste preview text into PRs or chats.",
            file=sys.stderr,
        )
    if show_observed_text:
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
    ) or args.command == "session"


def _handle_local_command(
    args: argparse.Namespace,
    data_root: Path,
    accounts: AccountStore,
) -> int | None:
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
        return 1 if result.status == "CANCELLED" else 0

    if args.command == "ls":
        listed_accounts = accounts.list()
        if not listed_accounts:
            print("No local accounts.")
        for account in listed_accounts:
            verification = "VERIFIED" if account.expected_actor is not None else "UNVERIFIED"
            print(f"{account.alias} {verification}")
        return 0

    if args.command == "recover":
        account = accounts.get(args.alias)
        try:
            confirmation = input(
                "Verify the prior managed Chromium process is closed. "
                "Type CLOSED to clear its cleanup block: "
            )
        except EOFError:
            confirmation = ""
        if confirmation != "CLOSED":
            print("CLEANUP_RECOVERY status=NOT_CONFIRMED", file=sys.stderr)
            return 1
        cleared = CheckpointStore(data_root).profile_lock(account).clear_cleanup_failed()
        status = "UNBLOCKED" if cleared else "NOT_REQUIRED"
        print(f"CLEANUP_RECOVERY status={status}")
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
    return None


def _validate_run_arguments(
    args: argparse.Namespace,
    *,
    session: bool,
    scheduled: bool,
    scheduled_status: dict[str, object] | None,
) -> bool:
    if args.command == "nurture" and not args.headed:
        print("ERROR HEADED_REQUIRED", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "HEADED_REQUIRED")
        return False
    if not 1 <= args.max_posts <= 20:
        print("ERROR POST_LIMIT_INVALID", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "POST_LIMIT_INVALID")
        return False
    if not 0 <= args.max_scrolls <= 20:
        print("ERROR SCROLL_LIMIT_INVALID", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "SCROLL_LIMIT_INVALID")
        return False
    if not 1 <= args.max_total_posts <= 400:
        print("ERROR TOTAL_SCAN_LIMIT_INVALID", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "TOTAL_SCAN_LIMIT_INVALID")
        return False
    if args.max_runtime_seconds is not None and (
        not math.isfinite(args.max_runtime_seconds) or args.max_runtime_seconds <= 0
    ):
        print("ERROR RUNTIME_LIMIT_INVALID", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "RUNTIME_LIMIT_INVALID")
        return False
    if session and not 10 < args.max_runtime_seconds <= 3600:
        print("ERROR RUNTIME_LIMIT_INVALID", file=sys.stderr)
        return False
    if session and not 1 <= args.max_detail_reads <= 40:
        print("ERROR DETAIL_READ_LIMIT_INVALID", file=sys.stderr)
        return False
    if scheduled and args.max_runtime_seconds is None:
        print("ERROR SCHEDULED_RUNTIME_REQUIRED", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "SCHEDULED_RUNTIME_REQUIRED")
        return False
    return True


def _prepare_run_inputs(
    args: argparse.Namespace,
    data_root: Path,
    accounts: AccountStore,
    scheduled: bool,
    scheduled_status: dict[str, object] | None,
) -> tuple[BrowserAccount, tuple[str, ...]] | None:
    account = accounts.get(args.alias)
    if account.expected_actor is None:
        print("ERROR ACCOUNT_NOT_VERIFIED", file=sys.stderr)
        _mark_scheduled_failure(scheduled_status, "ACCOUNT_NOT_VERIFIED")
        return None
    terms = tuple(args.terms) if args.terms else TermsStore(data_root).get(account.alias)
    if not terms:
        print("ERROR TERMS_NOT_CONFIGURED", file=sys.stderr)
        if not scheduled:
            print(
                f'Configure terms with: tb terms {account.alias} "hiring"',
                file=sys.stderr,
            )
        _mark_scheduled_failure(scheduled_status, "TERMS_NOT_CONFIGURED")
        return None
    return account, terms


def _invoke_nurture(
    args: argparse.Namespace,
    data_root: Path,
    account: BrowserAccount,
    terms: tuple[str, ...],
    *,
    session: bool,
    scheduled: bool,
) -> object:
    from .workflow import run_browser_nurture, run_browser_nurture_session

    checkpoint_store = CheckpointStore(data_root)
    if session:
        return asyncio.run(
            run_browser_nurture_session(
                account,
                checkpoint_store,
                max_posts=args.max_posts,
                terms=terms,
                max_scrolls=args.max_scrolls,
                max_total_posts=args.max_total_posts,
                max_detail_reads=args.max_detail_reads,
                max_runtime_seconds=args.max_runtime_seconds,
                readiness_prompt=(
                    _prompt_readiness_extension if not args.no_readiness_prompt else None
                ),
            )
        )
    return asyncio.run(
        run_browser_nurture(
            account,
            checkpoint_store,
            max_posts=args.max_posts,
            terms=terms,
            max_scrolls=args.max_scrolls,
            max_total_posts=args.max_total_posts,
            max_runtime_seconds=args.max_runtime_seconds,
            show_post_text=args.show_post_text,
            show_observed_text=args.show_observed_text,
            detail=args.detail,
            **(
                {"console_output_policy": ConsoleOutputPolicy.SCHEDULED}
                if scheduled
                else {}
            ),
            readiness_prompt=(
                _prompt_readiness_extension
                if not args.no_readiness_prompt and not scheduled and _has_interactive_terminal()
                else None
            ),
        )
    )


def _report_run_result(
    args: argparse.Namespace,
    result: object,
    *,
    session: bool,
    scheduled: bool,
    scheduled_status: dict[str, object] | None,
) -> int:
    _mark_scheduled_result(scheduled_status, result)
    formatted_result = _format_result(result)
    if session:
        formatted_result += (
            " checkpoint_saved="
            f"{str(getattr(result, 'checkpoint_saved', False) is True).lower()}"
        )
    print(formatted_result)
    session_status = getattr(result, "status", None)
    if session and session_status not in {"SESSION_COMPLETE", "SESSION_BUDGET_EXHAUSTED"}:
        reason = _safe_status_code(
            getattr(result, "stop_reason", None),
            default=_safe_status_code(session_status, default="SESSION_STOPPED"),
        )
        print(f"STOP {reason}", file=sys.stderr)
        return 1
    if (scheduled or session) and getattr(result, "checkpoint_saved", False) is not True:
        reason = _safe_status_code(
            getattr(result, "stop_reason", None),
            default=_safe_status_code(getattr(result, "status", None), default="CYCLE_NOT_CHECKPOINTED"),
        )
        print(f"STOP {reason}", file=sys.stderr)
        return 1
    return 0


def _handle_run_command(
    args: argparse.Namespace,
    data_root: Path,
    accounts: AccountStore,
    scheduled_status: dict[str, object] | None,
) -> int:
    session = args.command == "session"
    scheduled = getattr(args, "scheduled", False)
    if not _validate_run_arguments(
        args, session=session, scheduled=scheduled, scheduled_status=scheduled_status
    ):
        return 1
    run_inputs = _prepare_run_inputs(
        args, data_root, accounts, scheduled, scheduled_status
    )
    if run_inputs is None:
        return 1
    account, terms = run_inputs
    from .reader import UiReadError

    try:
        result = _invoke_nurture(
            args,
            data_root,
            account,
            terms,
            session=session,
            scheduled=scheduled,
        )
    except UiReadError as error:
        _mark_scheduled_failure(scheduled_status, error.code)
        _record_known_checkpoint_runs(scheduled_status, data_root, account, terms)
        if getattr(args, "detail", False):
            print(f"[CHECKPOINT] status=NOT_SAVED reason={error.code}", file=sys.stderr)
            print(f"[STOP] code={error.code}", file=sys.stderr)
        else:
            print(f"STOP {error.code}", file=sys.stderr)
        if error.code == "LOGIN_REQUIRED" and not scheduled:
            print(f"Run 'tb login {account.alias}' to authenticate.", file=sys.stderr)
        return 1
    except BrowserDataError as error:
        _mark_scheduled_failure(scheduled_status, error.code)
        _record_known_checkpoint_runs(scheduled_status, data_root, account, terms)
        print(f"ERROR {error.code}", file=sys.stderr)
        return 1
    return _report_run_result(
        args,
        result,
        session=session,
        scheduled=scheduled,
        scheduled_status=scheduled_status,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    session = args.command == "session"
    scheduled = _is_run(args) and getattr(args, "scheduled", False)
    if args.command == "recover" and not _has_interactive_terminal():
        print("ERROR CLEANUP_RECOVERY_TTY_REQUIRED", file=sys.stderr)
        return 1
    if scheduled and (
        getattr(args, "detail", False)
        or getattr(args, "show_post_text", False)
        or getattr(args, "show_observed_text", False)
    ):
        print("ERROR SCHEDULED_TEXT_OUTPUT_INVALID", file=sys.stderr)
        return 1
    if session and not _has_interactive_terminal():
        print("ERROR SESSION_ATTENDED_TERMINAL_REQUIRED", file=sys.stderr)
        return 1
    if _is_run(args) and not _run_diagnostics_check(args):
        return 1

    scheduled_status, scheduled_started = (
        _new_scheduled_status() if scheduled else (None, None)
    )
    data_root: Path | None = None
    scheduled_status_ready = False
    try:
        data_root = resolve_data_root()
        if scheduled_status is not None:
            write_operation_status(data_root, scheduled_status)
            scheduled_status_ready = True
        accounts = AccountStore(data_root)
        local_result = _handle_local_command(args, data_root, accounts)
        if local_result is not None:
            return local_result
        if _is_run(args):
            return _handle_run_command(args, data_root, accounts, scheduled_status)
    except BrowserDataError as error:
        _mark_scheduled_failure(scheduled_status, error.code)
        print(f"ERROR {error.code}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        _mark_scheduled_failure(scheduled_status, "INTERRUPTED")
        print("ERROR INTERRUPTED", file=sys.stderr)
        return 1
    finally:
        if scheduled_status_ready and scheduled_status is not None:
            assert scheduled_started is not None and data_root is not None
            _finish_scheduled_status(scheduled_status, scheduled_started)
            try:
                write_operation_status(data_root, scheduled_status)
            except BrowserDataError:
                print("WARNING SCHEDULED_STATUS_WRITE_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
