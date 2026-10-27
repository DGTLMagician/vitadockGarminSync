"""Command line entry point: ``vitadock-garmin-sync {login,sync,run}``."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .sync import State, Syncer
from .vitadock import AuthError, VitaDockClient, VitaDockError

log = logging.getLogger("vitadock_garmin_sync")


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def state_dir() -> Path:
    # systemd's StateDirectory= sets $STATE_DIRECTORY.
    d = env("STATE_DIRECTORY") or env("VDGS_STATE_DIR") or "~/.local/state/vitadock-garmin-sync"
    return Path(d).expanduser()


def garmin_client(interactive: bool):
    from garminconnect import Garmin

    tokens = state_dir() / "garmin"
    email, password = env("GARMIN_EMAIL"), env("GARMIN_PASSWORD")
    if interactive and not has_garmin_tokens():
        email = email or input("Garmin e-mail: ")
        password = password or getpass.getpass("Garmin password: ")
    g = Garmin(email, password, prompt_mfa=(lambda: input("Garmin MFA code: ")) if interactive else None)
    tokens.mkdir(parents=True, exist_ok=True, mode=0o700)
    g.login(str(tokens))
    return g


def has_garmin_tokens() -> bool:
    tokens = state_dir() / "garmin"
    return tokens.is_dir() and any(tokens.iterdir())


def cmd_login(args) -> int:
    if not args.garmin_only:
        state = State.load(state_dir() / "state.json")
        email = env("VITADOCK_EMAIL") or input("VitaDock e-mail: ")
        password = env("VITADOCK_PASSWORD") or getpass.getpass("VitaDock password: ")
        access = VitaDockClient(env("VITADOCK_SERVER", "eu")).login(email, password)
        state.access_token, state.access_secret = access.token, access.secret
        state.save()
        print("VitaDock: logged in, access token stored.")
    if not args.vitadock_only:
        garmin_client(interactive=True)
        print("Garmin Connect: logged in, tokens stored.")
    return 0


def make_syncer(dry_run: bool) -> Syncer:
    state = State.load(state_dir() / "state.json")
    vd = VitaDockClient(env("VITADOCK_SERVER", "eu"), state.access)
    if state.access is None:
        if not (env("VITADOCK_EMAIL") and env("VITADOCK_PASSWORD")):
            raise SystemExit("Not logged in to VitaDock: run 'vitadock-garmin-sync login' first.")
        access = vd.login(env("VITADOCK_EMAIL"), env("VITADOCK_PASSWORD"))
        state.access_token, state.access_secret = access.token, access.secret
        state.save()
    tz = ZoneInfo(env("VDGS_TIMEZONE", "Europe/Amsterdam"))
    since = env("VDGS_SYNC_SINCE")
    return Syncer(
        vitadock=vd,
        # A dry run still reads Garmin (to skip what is already there) when logged in.
        garmin=None if dry_run and not has_garmin_tokens() else garmin_client(interactive=sys.stdin.isatty()),
        state=state,
        tz=tz,
        wallclock=env("VDGS_TIMESTAMPS", "utc") == "wallclock",
        initial_since=datetime.fromisoformat(since).replace(tzinfo=tz) if since else None,
        dry_run=dry_run,
        modules=tuple(m.strip() for m in env("VDGS_MODULES", "targetscales,cardiodocks").split(",")),
    )


def sync_once(dry_run: bool) -> int:
    syncer = make_syncer(dry_run)
    try:
        n = syncer.run()
    except AuthError:
        # Token revoked (e.g. logout elsewhere): log in again if we can.
        if not (env("VITADOCK_EMAIL") and env("VITADOCK_PASSWORD")):
            raise
        log.warning("VitaDock token rejected, logging in again")
        syncer.state.access_token = syncer.state.access_secret = None
        syncer.state.save()
        syncer = make_syncer(dry_run)
        n = syncer.run()
    log.info("%s %d measurement(s)", "would upload" if dry_run else "uploaded", n)
    return n


def cmd_sync(args) -> int:
    sync_once(args.dry_run)
    return 0


def cmd_run(args) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        try:
            sync_once(False)
        except Exception:
            log.exception("sync failed")
        time.sleep(args.interval * 60)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vitadock-garmin-sync",
                                description="Sync VitaDock+ (Medisana) measurements to Garmin Connect.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("login", help="log in to VitaDock and Garmin and store tokens")
    only = lp.add_mutually_exclusive_group()
    only.add_argument("--vitadock-only", action="store_true")
    only.add_argument("--garmin-only", action="store_true")
    lp.set_defaults(func=cmd_login)
    sp = sub.add_parser("sync", help="sync once (for cron or a systemd timer)")
    sp.add_argument("-n", "--dry-run", action="store_true", help="show what would be uploaded")
    sp.set_defaults(func=cmd_sync)
    rp = sub.add_parser("run", help="sync in a loop (daemon mode)")
    rp.add_argument("--interval", type=int, default=int(env("VDGS_INTERVAL_MINUTES", "30")),
                    help="minutes between syncs (default 30)")
    rp.set_defaults(func=cmd_run)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except VitaDockError as e:
        if args.verbose:
            raise
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
