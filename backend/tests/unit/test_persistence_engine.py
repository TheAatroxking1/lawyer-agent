from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.engine import make_url

from lawyer_agent.infrastructure.persistence import engine as persistence


@pytest.mark.parametrize(
    ("platform", "driver", "expected_driver"),
    [
        ("win32", "mysql+asyncmy", "mysql+aiomysql"),
        ("linux", "mysql+asyncmy", "mysql+asyncmy"),
        ("darwin", "mysql+asyncmy", "mysql+asyncmy"),
        ("win32", "mysql+aiomysql", "mysql+aiomysql"),
    ],
)
def test_engine_selects_windows_stream_driver_without_changing_connection_settings(
    monkeypatch: pytest.MonkeyPatch, platform: str, driver: str, expected_driver: str
) -> None:
    url = make_url(f"{driver}://sample:p%40ss%2Fword@localhost:13307/sample?charset=utf8mb4")
    create = MagicMock()
    listen = MagicMock()
    monkeypatch.setattr(persistence, "sys", SimpleNamespace(platform=platform), raising=False)
    monkeypatch.setattr(persistence, "create_async_engine", create)
    monkeypatch.setattr(persistence.event, "listens_for", listen)

    result = persistence.create_engine_from_url(
        url.render_as_string(hide_password=False),
        pool_size=3,
        max_overflow=2,
        pool_timeout_seconds=7.5,
    )

    actual_url = make_url(create.call_args.args[0])
    assert actual_url == url.set(drivername=expected_driver)
    assert create.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 7.5,
    }
    assert result is create.return_value
    listen.assert_called_once_with(result.sync_engine, "connect")
    configure = listen.return_value.call_args.args[0]
    connection = MagicMock()
    original_escape = connection.driver_connection.escape
    original_escape.return_value = "'escaped text'"
    configure(connection, object())
    connection.cursor.return_value.execute.assert_called_once_with("SET time_zone = '+00:00'")
    connection.cursor.return_value.close.assert_called_once()
    if expected_driver == "mysql+aiomysql":
        assert connection.driver_connection.escape(b"\x00\xff'\\") == "_binary X'00ff275c'"
        original_escape.assert_not_called()
        assert connection.driver_connection.escape("ordinary text") == "'escaped text'"
        original_escape.assert_called_once_with("ordinary text")
    else:
        assert connection.driver_connection.escape is original_escape
