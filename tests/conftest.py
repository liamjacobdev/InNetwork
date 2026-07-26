"""Shared fixtures: every test runs against an isolated temp SQLite DB."""
import os
import tempfile

import pytest

from app import db
from app.config import settings


@pytest.fixture(autouse=True)
def _hermetic_db(tmp_path_factory):
    """Point the datastore at an empty temp DB for EVERY test.

    Without this, any test that doesn't request `temp_db` reads the developer's real
    ./innetwork.db — so a locally-ingested Medicare index silently supplies a verified
    payer that CI, which has no such file, does not have. That is not hypothetical: it let
    a test asserting "a national plan survives state scoping" pass on a dev box and fail on
    a clean checkout, because the national plan it checked was ambient rather than created
    by the test.

    `temp_db` still overrides this per-test for the tests that want a real initialized
    schema; this just makes the DEFAULT hermetic instead of whatever happens to be on disk.
    """
    old = settings.db_path
    settings.db_path = str(tmp_path_factory.mktemp("hermetic_db") / "test.db")
    try:
        yield
    finally:
        settings.db_path = old


@pytest.fixture(autouse=True)
def _hermetic_planet_registry():
    """Keep the suite hermetic: don't auto-wire the live public Plan-Net endpoints (no
    real network calls / 12s timeouts during tests). Tests that exercise the registry
    set settings.use_planet_registry = True explicitly."""
    old = settings.use_planet_registry
    settings.use_planet_registry = False
    try:
        yield
    finally:
        settings.use_planet_registry = old


@pytest.fixture(autouse=True)
def _hermetic_membership(tmp_path_factory):
    """Point the membership store at an empty temp dir so tests don't load the committed
    payers/ bitmaps (e.g. the 5MB Medicare blob). Membership tests build their own store
    in a tmp dir and load it directly; registry tests that want a bitmap set
    settings.membership_dir explicitly."""
    old = settings.membership_dir
    settings.membership_dir = str(tmp_path_factory.mktemp("empty_membership"))
    try:
        yield
    finally:
        settings.membership_dir = old


@pytest.fixture()
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old = settings.db_path
    settings.db_path = path
    db.init_db()
    try:
        yield path
    finally:
        settings.db_path = old
        for p in (path, path + "-wal", path + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
