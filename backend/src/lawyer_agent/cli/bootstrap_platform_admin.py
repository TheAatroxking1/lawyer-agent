from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.platform import (
    BootstrapAuthenticationFailed,
    BootstrapCommittedWithCleanupWarning,
    BootstrapPlatformAdminCommand,
    BootstrapSecretVerifier,
    BootstrapUnavailable,
    PlatformBootstrapService,
)
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import require_uuid7

_MAX_SECRET_FILE_BYTES = 4096


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lawyer_agent.cli.bootstrap_platform_admin",
        description="Assign the first platform super administrator.",
    )
    parser.add_argument("--user-id", required=True, help="Existing registered UUIDv7 user")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--secret-stdin",
        action="store_true",
        help="Read the bootstrap secret from standard input",
    )
    source.add_argument(
        "--secret-file",
        type=Path,
        help="Read the bootstrap secret from a mounted Secret file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        user_id = UUID(args.user_id)
        require_uuid7(user_id, field="bootstrap user_id")
        secret = _read_secret(from_stdin=args.secret_stdin, path=args.secret_file)
        expected_digest = os.environ["LAWYER_BOOTSTRAP_ADMIN_SECRET_DIGEST"]
        verifier = BootstrapSecretVerifier.from_hex_digest(expected_digest)
    except (KeyError, OSError, UnicodeError, ValueError):
        print("bootstrap configuration is invalid", file=sys.stderr)
        return 2

    try:
        asyncio.run(_bootstrap(user_id=user_id, secret=secret, verifier=verifier))
    except BootstrapCommittedWithCleanupWarning:
        print("platform administrator bootstrap completed with cleanup warning")
        return 0
    except BootstrapAuthenticationFailed:
        print("bootstrap authentication failed", file=sys.stderr)
        return 3
    except BootstrapUnavailable:
        print("bootstrap is unavailable", file=sys.stderr)
        return 4
    except Exception:
        print("bootstrap failed", file=sys.stderr)
        return 1
    print("platform administrator bootstrap completed")
    return 0


async def _bootstrap(
    *,
    user_id: UUID,
    secret: str,
    verifier: BootstrapSecretVerifier,
) -> None:
    from lawyer_agent.infrastructure.persistence.engine import (
        create_engine,
        create_session_factory,
    )
    from lawyer_agent.infrastructure.persistence.platform_uow import (
        SqlAlchemyPlatformWorkflowUnitOfWork,
    )

    settings = Settings()
    engine = create_engine(settings)
    try:
        service = PlatformBootstrapService(
            uow_factory=lambda: SqlAlchemyPlatformWorkflowUnitOfWork(
                create_session_factory(engine)
            ),
            verifier=verifier,
        )
        await service.bootstrap(
            BootstrapPlatformAdminCommand(
                user_id=user_id,
                secret=secret,
                audit_context=AuditContext(
                    trace_id="platform-admin-bootstrap-cli",
                    client_ip_hash=None,
                    user_agent_hash=None,
                ),
            )
        )
    finally:
        await engine.dispose()


def _read_secret(*, from_stdin: bool, path: Path | None) -> str:
    if from_stdin:
        raw = sys.stdin.read(_MAX_SECRET_FILE_BYTES + 1)
    elif path is not None:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(_MAX_SECRET_FILE_BYTES + 1)
    else:
        raise ValueError("bootstrap secret source is missing")
    if len(raw.encode("utf-8")) > _MAX_SECRET_FILE_BYTES:
        raise ValueError("bootstrap secret is too large")
    secret = raw.rstrip("\r\n")
    if not secret:
        raise ValueError("bootstrap secret is empty")
    return secret


if __name__ == "__main__":
    raise SystemExit(main())
