import pytest

import quahog
from quahog.session import Session


@pytest.fixture(autouse=True)
def shell_env(tmp_path):
    """Sessions are real login shells, so need to override certain environments.

    This fixture overrides the default environment."""
    try:
        Session._DEFAULT_ENV = (out := {"HISTFILE": str(tmp_path / "history")})
        yield out
    finally:
        Session._DEFAULT_ENV = {}


@pytest.fixture()
def sh():
    s = quahog.bash()
    try:
        yield s
    finally:
        quahog.sessions.pop(s.name, None)
        s.close()


@pytest.fixture()
def zs():
    s = quahog.zsh()
    try:
        yield s
    finally:
        quahog.sessions.pop(s.name, None)
        s.close()
