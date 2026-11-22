"""Bootstrap the first admin, issue invitations and block user accounts locally."""

import argparse
import getpass
import sqlite3
import sys
import warnings

from .auth_config import AuthConfig
from .auth_models import AccountInput, Credentials
from .auth_store import AuthError, AuthStore
from .config import load_project_env


def read_password(from_stdin, *, confirm=False):
    if from_stdin:
        password = sys.stdin.readline(516).rstrip("\r\n")
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Password: ")
            if confirm and password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
    return password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap-admin")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--display-name", required=True)
    invitation = sub.add_parser("invite")
    invitation.add_argument("--admin-email", required=True)
    invitation.add_argument("--ttl-seconds", type=int, default=86400)
    block = sub.add_parser("block")
    block.add_argument("--admin-email", required=True)
    block.add_argument("--user-id", required=True)
    for command in (bootstrap, invitation, block):
        command.add_argument("--password-stdin", action="store_true",
                             help="Read from a secret pipe instead of a hidden interactive prompt")
    args = parser.parse_args()
    load_project_env()
    try:
        store = AuthStore(AuthConfig.from_env())
        store.initialize()
        password = read_password(args.password_stdin, confirm=args.command == "bootstrap-admin")
        if args.command == "bootstrap-admin":
            account = AccountInput(email=args.email, display_name=args.display_name, password=password)
            print(store.add_user(account, bootstrap=True).user_id)
        else:
            account = Credentials(email=args.admin_email, password=password)
            admin = store.admin_credentials(account.email, account.password.get_secret_value())
            if args.command == "invite":
                print(store.invite(admin, args.ttl_seconds))
            else:
                store.block(admin, args.user_id)
    except AuthError as exc:
        parser.exit(1, f"{exc.code}\n")
    except (ValueError, OSError, sqlite3.Error, getpass.GetPassWarning):
        parser.exit(1, "Could not complete command. Check account fields, secret input and multiuser configuration.\n")


if __name__ == "__main__":
    main()
