"""Argument definitions for the short and legacy command trees."""

from __future__ import annotations

import argparse
from pathlib import Path


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
        "--max-runtime-seconds",
        type=float,
        help="optional maximum browser workflow runtime in seconds",
    )
    parser.add_argument(
        "--no-readiness-prompt",
        action="store_true",
        help="stop when readiness checks expire instead of waiting for operator input",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="write a sanitized latest-run summary and suppress operator readiness prompts",
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

    recover = commands.add_parser(
        "recover",
        help="clear an account's browser cleanup block after attended process verification",
    )
    recover.add_argument("alias")

    terms = commands.add_parser("terms", help="view or replace an account's nurture terms")
    terms.add_argument("alias")
    terms.add_argument("terms", nargs="*", help="replacement relevance terms")
    terms.add_argument("--clear", action="store_true", help="clear saved relevance terms")

    run = commands.add_parser("run", help="run bounded, read-only nurture")
    run.add_argument("alias")
    _add_run_options(run, legacy=False)

    session = commands.add_parser(
        "session",
        help="continue bounded feed observations and verified Detail reads in one headed session",
    )
    session.add_argument("alias")
    session.add_argument(
        "--max-posts",
        type=int,
        default=20,
        help="maximum visible post rows per viewport (1-20; default: 20)",
    )
    session.add_argument(
        "--max-scrolls",
        required=True,
        type=int,
        help="maximum measured feed advances for the whole browser session (0-20)",
    )
    session.add_argument(
        "--max-total-posts",
        required=True,
        type=int,
        help="maximum distinct verified posts for the whole session (1-400)",
    )
    session.add_argument(
        "--max-detail-reads",
        required=True,
        type=int,
        help="maximum Detail reads for the whole session (1-40)",
    )
    session.add_argument(
        "--max-runtime-seconds",
        required=True,
        type=float,
        help="maximum total session duration including cleanup (>10 and <=3600)",
    )
    session.add_argument(
        "--no-readiness-prompt",
        action="store_true",
        help="stop when readiness checks expire instead of waiting for operator input",
    )
    session.add_argument(
        "-t",
        "--term",
        action="append",
        dest="terms",
        help="one-time local relevance term; repeat for more terms",
    )

    auto = commands.add_parser(
        "auto",
        help="continuously refresh and read verified hiring and job-seeking posts",
    )
    auto.add_argument("alias")
    auto.add_argument(
        "--max-cycles",
        type=int,
        help="stop after this many bounded refresh cycles (default: continue until interrupted)",
    )

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
