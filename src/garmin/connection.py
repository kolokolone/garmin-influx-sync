"""Authenticated Garmin Personal API sessions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator

from config import Settings


@contextmanager
def garmin_session(settings: Settings) -> Iterator[GarminPersonalApiClient]:
    """Yield an authenticated client for one unit of work.

    The authenticator caches its bearer token on disk, so a session only hits
    the token endpoint when the cached token is missing or about to expire.
    """
    with PersonalApiAuthenticator() as auth:
        token = auth.obtain_token(settings.garmin_api_key)
    with GarminPersonalApiClient(token.access_token) as client:
        yield client
