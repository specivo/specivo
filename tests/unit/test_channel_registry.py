"""Unit tests for the notification channel registry.

RED phase — these tests define the expected behaviour of the module-level
channel registry before any implementation exists.

Covers:
- register_channel() adds a channel
- Duplicate registration raises ValueError
- get_channel() returns registered channel
- get_channel() returns None for unknown key
- get_all_channels() returns all registered channels
- channel_keys() returns sorted list

IMPORTANT: The registry is a module-level dict. Each test class uses an
autouse fixture to save and restore registry state so tests are isolated.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(key: str) -> MagicMock:
    """Build a minimal mock that satisfies the NotificationChannel interface."""
    channel = MagicMock()
    type(channel).channel_key = PropertyMock(return_value=key)
    return channel


# ---------------------------------------------------------------------------
# Registry isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Save and restore the module-level _channels dict around each test."""
    from specivo.services.channels import registry as reg_module

    original = dict(reg_module._channels)
    reg_module._channels.clear()
    yield
    reg_module._channels.clear()
    reg_module._channels.update(original)


# ---------------------------------------------------------------------------
# register_channel
# ---------------------------------------------------------------------------


class TestRegisterChannel:
    def test_register_adds_channel(self):
        """register_channel() stores the channel under its key."""
        from specivo.services.channels.registry import get_channel, register_channel

        ch = _make_channel("email")
        register_channel(ch)
        assert get_channel("email") is ch

    def test_duplicate_registration_raises_value_error(self):
        """Registering the same key twice raises ValueError."""
        from specivo.services.channels.registry import register_channel

        register_channel(_make_channel("email"))
        with pytest.raises(ValueError, match="email"):
            register_channel(_make_channel("email"))

    def test_two_different_channels_can_be_registered(self):
        """Different keys can both be registered without conflict."""
        from specivo.services.channels.registry import get_channel, register_channel

        email_ch = _make_channel("email")
        telegram_ch = _make_channel("telegram")
        register_channel(email_ch)
        register_channel(telegram_ch)
        assert get_channel("email") is email_ch
        assert get_channel("telegram") is telegram_ch


# ---------------------------------------------------------------------------
# get_channel
# ---------------------------------------------------------------------------


class TestGetChannel:
    def test_returns_registered_channel(self):
        """get_channel() returns the channel that was registered."""
        from specivo.services.channels.registry import get_channel, register_channel

        ch = _make_channel("email")
        register_channel(ch)
        assert get_channel("email") is ch

    def test_returns_none_for_unknown_key(self):
        """get_channel() returns None when the key has never been registered."""
        from specivo.services.channels.registry import get_channel

        assert get_channel("nonexistent") is None

    def test_returns_none_for_empty_registry(self):
        """get_channel() returns None when the registry is empty."""
        from specivo.services.channels.registry import get_channel

        assert get_channel("email") is None


# ---------------------------------------------------------------------------
# get_all_channels
# ---------------------------------------------------------------------------


class TestGetAllChannels:
    def test_returns_empty_dict_when_no_channels_registered(self):
        """get_all_channels() returns {} when the registry is empty."""
        from specivo.services.channels.registry import get_all_channels

        result = get_all_channels()
        assert result == {}

    def test_returns_all_registered_channels(self):
        """get_all_channels() returns a dict of all registered channels."""
        from specivo.services.channels.registry import get_all_channels, register_channel

        email_ch = _make_channel("email")
        telegram_ch = _make_channel("telegram")
        register_channel(email_ch)
        register_channel(telegram_ch)

        result = get_all_channels()
        assert len(result) == 2
        assert result["email"] is email_ch
        assert result["telegram"] is telegram_ch

    def test_returns_copy_not_reference(self):
        """Mutating the returned dict must not affect the registry."""
        from specivo.services.channels.registry import get_all_channels, register_channel

        register_channel(_make_channel("email"))
        result = get_all_channels()
        result.pop("email")  # mutate the returned copy

        # Original registry is unaffected
        fresh = get_all_channels()
        assert "email" in fresh


# ---------------------------------------------------------------------------
# channel_keys
# ---------------------------------------------------------------------------


class TestChannelKeys:
    def test_returns_empty_list_when_registry_empty(self):
        """channel_keys() returns [] when nothing is registered."""
        from specivo.services.channels.registry import channel_keys

        assert channel_keys() == []

    def test_returns_sorted_keys(self):
        """channel_keys() returns channel keys in sorted order."""
        from specivo.services.channels.registry import channel_keys, register_channel

        register_channel(_make_channel("telegram"))
        register_channel(_make_channel("discord"))
        register_channel(_make_channel("email"))

        keys = channel_keys()
        assert keys == sorted(keys)

    def test_returns_all_keys(self):
        """channel_keys() returns a key for every registered channel."""
        from specivo.services.channels.registry import channel_keys, register_channel

        register_channel(_make_channel("email"))
        register_channel(_make_channel("telegram"))

        keys = channel_keys()
        assert "email" in keys
        assert "telegram" in keys
        assert len(keys) == 2
