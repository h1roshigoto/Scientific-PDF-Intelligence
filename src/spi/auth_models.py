"""Account API models and shared validation for HTTP and CLI."""

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

USER_ID = r"^[a-f0-9]{32}$"
SECRET = r"^[A-Za-z0-9_-]{43}$"
Password = Annotated[SecretStr, Field(min_length=1, max_length=128)]
Email = Annotated[str, Field(min_length=3, max_length=254)]
Name = Annotated[str, Field(min_length=1, max_length=80)]


def normalized_email(value: str) -> str:
    # This initial account system accepts ASCII mailbox addresses and uses a
    # case-insensitive lookup for the complete address, without alias rewriting.
    local, separator, domain = value.partition("@")
    labels = domain.split(".")
    if (not separator or len(local) > 64 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local or len(labels) < 2
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in labels)):
        raise ValueError("Invalid email address")
    return value.lower()


def strong_password(password: str, email: str = "") -> None:
    if not 15 <= len(password) <= 128 or len(password.encode()) > 512:
        raise ValueError("Use a password of 15–128 characters")
    folded = password.casefold()
    compact = "".join(char for char in folded if char.isalnum())
    local = email.partition("@")[0].lower()
    weak = {"password", "qwerty", "letmein", "iloveyou", "123456", "changeme", "adminadmin",
            "correcthorsebatterystaple"}
    if (len(set(compact)) < 5 or any(item in compact for item in weak)
            or (len(local) >= 5 and local in folded)):
        raise ValueError("Choose a less predictable password")


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: Email
    password: Password

    @field_validator("email")
    @classmethod
    def email_valid(cls, email):
        normalized_email(email)
        return email


class AccountInput(Credentials):
    display_name: Name

    @field_validator("display_name")
    @classmethod
    def display_name_valid(cls, value):
        if not value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Invalid display name")
        return value.strip()


class Registration(AccountInput):
    invitation_code: Annotated[SecretStr, Field(min_length=43, max_length=43)]


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    current_password: Password
    new_password: Password


class User(BaseModel):
    user_id: str
    email: str
    display_name: str
    status: Literal["active", "blocked"]
    role: Literal["user", "admin"]
    created_at: int
    updated_at: int
    last_login_at: int | None
    email_verified: Literal[False] = False


class AccountSession(User):
    session_id: str
