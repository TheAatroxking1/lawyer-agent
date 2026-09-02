from __future__ import annotations

from base64 import b64encode
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lawyer_agent.api.dependencies import application_services
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import AccessTokenClaims, Audience
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService


def _seed(byte: int) -> bytes:
    return bytes([byte]) * 32


def _encoded_seed(byte: int) -> str:
    return b64encode(_seed(byte)).decode("ascii")


def _claims(now: datetime) -> AccessTokenClaims:
    return AccessTokenClaims(
        user_id=new_uuid7(),
        session_id=new_uuid7(),
        token_id=new_uuid7(),
        audience=Audience.ACCOUNT,
        issued_at=now,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        auth_version=1,
    )


@pytest.mark.asyncio
async def test_composition_uses_exact_rfc8032_seeds_and_keeps_old_verifiers() -> None:
    settings = Settings(
        environment="test",
        jwt_active_kid="active",
        jwt_ed25519_key_ring={"old": _encoded_seed(21), "active": _encoded_seed(22)},
    )
    old_key = Ed25519PrivateKey.from_private_bytes(_seed(21))
    active_key = Ed25519PrivateKey.from_private_bytes(_seed(22))
    now = datetime.now(UTC).replace(microsecond=0)

    async with application_services(settings) as services:
        active_token = services.token_service.issue_account(_claims(now))
        exact_seed_verifier = TokenService(
            issuer=settings.jwt_issuer,
            active_kid="active",
            signing_keys={"active": active_key},
            verification_keys={
                "old": old_key.public_key(),
                "active": active_key.public_key(),
            },
        )
        assert exact_seed_verifier.verify(
            active_token,
            audience=Audience.ACCOUNT,
            now=now,
        ).audience is Audience.ACCOUNT

        old_signer = TokenService(
            issuer=settings.jwt_issuer,
            active_kid="old",
            signing_keys={"old": old_key},
            verification_keys={
                "old": old_key.public_key(),
                "active": active_key.public_key(),
            },
        )
        old_token = old_signer.issue_account(_claims(now))
        assert services.token_service.verify(
            old_token,
            audience=Audience.ACCOUNT,
            now=now,
        ).audience is Audience.ACCOUNT


@pytest.mark.asyncio
async def test_composition_accepts_rotation_ready_versioned_cipher_and_blind_rings() -> None:
    settings = Settings(
        environment="test",
        data_encryption_key_ring={7: _encoded_seed(31), 8: _encoded_seed(32)},
        data_encryption_active_key_version=8,
        blind_index_key_ring={7: _encoded_seed(33), 8: _encoded_seed(34)},
        blind_index_active_key_version=8,
        blind_index_rollout_phase="rotation-ready",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=True,
    )

    async with application_services(settings) as services:
        assert services.identity is not None
