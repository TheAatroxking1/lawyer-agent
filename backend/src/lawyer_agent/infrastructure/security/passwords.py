from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_CHARACTERS: Final = 12
MAX_PASSWORD_CHARACTERS: Final = 128
MAX_PASSWORD_UTF8_BYTES: Final = 1024

ARGON2_MEMORY_COST: Final = 65536
ARGON2_TIME_COST: Final = 3
ARGON2_PARALLELISM: Final = 1
ARGON2_HASH_LENGTH: Final = 32
ARGON2_SALT_LENGTH: Final = 16

_DUMMY_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=1$N3pQ+Xvo3Fo/d8Dbqwma/g$"
    "g32MhYLZErgb32zGER2vnamXD+F2kRT+2N2urXbpDOA"
)  # noqa: S105


class InvalidPassword(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    valid: bool
    needs_rehash: bool


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            memory_cost=ARGON2_MEMORY_COST,
            time_cost=ARGON2_TIME_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LENGTH,
            salt_len=ARGON2_SALT_LENGTH,
            type=Type.ID,
        )

    @property
    def parameters(self) -> dict[str, int | str]:
        return {
            "algorithm": "argon2id",
            "memory_cost": ARGON2_MEMORY_COST,
            "time_cost": ARGON2_TIME_COST,
            "parallelism": ARGON2_PARALLELISM,
            "hash_len": ARGON2_HASH_LENGTH,
            "salt_len": ARGON2_SALT_LENGTH,
        }

    def hash(self, password: str) -> str:
        self._validate_new_password(password)
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> PasswordVerification:
        try:
            valid = self._hasher.verify(encoded_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return PasswordVerification(valid=False, needs_rehash=False)
        if not valid:
            return PasswordVerification(valid=False, needs_rehash=False)
        return PasswordVerification(
            valid=True,
            needs_rehash=self._hasher.check_needs_rehash(encoded_hash),
        )

    def verify_dummy(self, password: str) -> None:
        self.verify(_DUMMY_HASH, password)

    @staticmethod
    def _validate_new_password(password: str) -> None:
        character_count = len(password)
        if not MIN_PASSWORD_CHARACTERS <= character_count <= MAX_PASSWORD_CHARACTERS:
            raise InvalidPassword("password must contain 12 to 128 characters")
        if len(password.encode("utf-8")) > MAX_PASSWORD_UTF8_BYTES:
            raise InvalidPassword("password UTF-8 representation exceeds 1024 bytes")
