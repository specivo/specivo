"""Unit tests for ProfileService — avatar color and photo upload logic.

All tests are pure — no database required.  Service methods that query
the DB are tested via mocking (AsyncMock / MagicMock).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from specivo.core.exceptions import ValidationError
from specivo.services.profile_service import ProfileService
from specivo.testing.factories.user import UserFactory

# ---------------------------------------------------------------------------
# Helpers — minimal valid image bytes
# ---------------------------------------------------------------------------

# Real JPEG SOI + APP0 marker (enough for magic-byte check)
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 12

# Minimal PNG header (8 bytes)
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

# Minimal WebP header: RIFF + 4-byte size + WEBP
_WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8

# Something that is definitely not an image
_GARBAGE_BYTES = b"notanimage" + b"\x00" * 20


# ---------------------------------------------------------------------------
# _verify_magic_bytes — static method, no DB required
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerifyMagicBytes:
    def test_jpeg_magic_bytes_match(self):
        assert ProfileService._verify_magic_bytes(_JPEG_BYTES, "image/jpeg") is True

    def test_png_magic_bytes_match(self):
        assert ProfileService._verify_magic_bytes(_PNG_BYTES, "image/png") is True

    def test_webp_magic_bytes_match(self):
        assert ProfileService._verify_magic_bytes(_WEBP_BYTES, "image/webp") is True

    def test_jpeg_bytes_rejected_for_png_mime(self):
        """JPEG content declared as PNG must be rejected."""
        assert ProfileService._verify_magic_bytes(_JPEG_BYTES, "image/png") is False

    def test_png_bytes_rejected_for_jpeg_mime(self):
        """PNG content declared as JPEG must be rejected."""
        assert ProfileService._verify_magic_bytes(_PNG_BYTES, "image/jpeg") is False

    def test_garbage_bytes_rejected_for_jpeg(self):
        assert ProfileService._verify_magic_bytes(_GARBAGE_BYTES, "image/jpeg") is False

    def test_garbage_bytes_rejected_for_png(self):
        assert ProfileService._verify_magic_bytes(_GARBAGE_BYTES, "image/png") is False

    def test_garbage_bytes_rejected_for_webp(self):
        assert ProfileService._verify_magic_bytes(_GARBAGE_BYTES, "image/webp") is False

    def test_webp_riff_without_webp_marker_rejected(self):
        """RIFF header that lacks the WEBP four-CC at offset 8 must be rejected."""
        bad_riff = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"\x00" * 8
        assert ProfileService._verify_magic_bytes(bad_riff, "image/webp") is False

    def test_empty_bytes_rejected(self):
        assert ProfileService._verify_magic_bytes(b"", "image/jpeg") is False


# ---------------------------------------------------------------------------
# update_avatar_color — requires mocked SettingsService.get_avatar_palette
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpdateAvatarColor:
    @pytest.fixture()
    def service(self):
        return ProfileService()

    @pytest.fixture()
    def user(self):
        return UserFactory.build(preferences={})

    @pytest.fixture()
    def mock_session(self):
        return AsyncMock()

    async def test_valid_color_updates_preferences(self, service, user, mock_session):
        """A color present in the palette is persisted in user.preferences."""
        palette = ["#5B8C5A", "#7B68AE", "#c49a3c"]
        with patch(
            "specivo.services.profile_service.SettingsService.get_avatar_palette",
            new=AsyncMock(return_value=palette),
        ):
            await service.update_avatar_color(mock_session, user, "#5B8C5A")

        assert user.preferences["avatar_color"] == "#5B8C5A"

    async def test_second_call_overwrites_existing_color(self, service, user, mock_session):
        """Calling update_avatar_color twice applies the latest color."""
        palette = ["#5B8C5A", "#7B68AE"]
        with patch(
            "specivo.services.profile_service.SettingsService.get_avatar_palette",
            new=AsyncMock(return_value=palette),
        ):
            await service.update_avatar_color(mock_session, user, "#5B8C5A")
            await service.update_avatar_color(mock_session, user, "#7B68AE")

        assert user.preferences["avatar_color"] == "#7B68AE"

    async def test_invalid_color_raises_validation_error(self, service, user, mock_session):
        """A color NOT in the palette must raise ValidationError."""
        palette = ["#c49a3c"]
        with patch(
            "specivo.services.profile_service.SettingsService.get_avatar_palette",
            new=AsyncMock(return_value=palette),
        ):
            with pytest.raises(ValidationError, match="Invalid avatar color"):
                await service.update_avatar_color(mock_session, user, "#FF0000")

    async def test_invalid_color_does_not_mutate_preferences(self, service, user, mock_session):
        """Failed update must leave user.preferences unchanged."""
        user.preferences = {"avatar_color": "#c49a3c"}
        palette = ["#c49a3c"]
        with patch(
            "specivo.services.profile_service.SettingsService.get_avatar_palette",
            new=AsyncMock(return_value=palette),
        ):
            with pytest.raises(ValidationError):
                await service.update_avatar_color(mock_session, user, "#BADBAD")

        assert user.preferences["avatar_color"] == "#c49a3c"

    async def test_color_with_existing_preferences_preserved(self, service, mock_session):
        """update_avatar_color merges into existing prefs, not replacing the dict."""
        user = UserFactory.build(preferences={"theme": "dark", "avatar_color": "#old"})
        palette = ["#5B8C5A"]
        with patch(
            "specivo.services.profile_service.SettingsService.get_avatar_palette",
            new=AsyncMock(return_value=palette),
        ):
            await service.update_avatar_color(mock_session, user, "#5B8C5A")

        assert user.preferences["theme"] == "dark"
        assert user.preferences["avatar_color"] == "#5B8C5A"


# ---------------------------------------------------------------------------
# upload_avatar — size, MIME, and magic-byte guards
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUploadAvatar:
    @pytest.fixture()
    def service(self):
        return ProfileService()

    @pytest.fixture()
    def user(self):
        return UserFactory.build(avatar_url=None)

    @pytest.fixture()
    def mock_session(self):
        return AsyncMock()

    async def test_file_too_large_raises_validation_error(self, service, user, mock_session):
        """Content exceeding avatar_max_size_mb must raise ValidationError."""
        # Default max is 5 MB; send 6 MB of zeros
        oversized = b"\x00" * (6 * 1024 * 1024)
        with pytest.raises(ValidationError, match="too large"):
            await service.upload_avatar(mock_session, user, oversized, "image/jpeg", "big.jpg")

    async def test_invalid_mime_type_raises_validation_error(self, service, user, mock_session):
        """An unsupported MIME type (e.g. image/gif) must raise ValidationError."""
        with pytest.raises(ValidationError, match="JPEG, PNG"):
            await service.upload_avatar(mock_session, user, b"\x00" * 100, "image/gif", "anim.gif")

    async def test_text_mime_type_raises_validation_error(self, service, user, mock_session):
        """text/plain must be rejected regardless of size."""
        with pytest.raises(ValidationError, match="JPEG, PNG"):
            await service.upload_avatar(mock_session, user, b"hello", "text/plain", "readme.txt")

    async def test_magic_byte_mismatch_raises_validation_error(self, service, user, mock_session):
        """Content whose magic bytes do not match the declared MIME type is rejected."""
        # Declare JPEG but send PNG bytes
        with pytest.raises(ValidationError):
            await service.upload_avatar(mock_session, user, _PNG_BYTES + b"\x00" * 100, "image/jpeg", "sneaky.jpg")

    async def test_garbage_content_raises_validation_error(self, service, user, mock_session):
        """Random bytes declared as JPEG must fail at magic-byte or Pillow verification."""
        with pytest.raises(ValidationError):
            await service.upload_avatar(mock_session, user, _GARBAGE_BYTES, "image/jpeg", "fake.jpg")


# ---------------------------------------------------------------------------
# delete_avatar
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeleteAvatar:
    async def test_delete_clears_avatar_url(self):
        """delete_avatar sets avatar_url to None."""
        user = UserFactory.build(avatar_url="/data/avatars/ab/abcdef.webp")
        session = AsyncMock()

        svc = ProfileService()
        with patch.object(ProfileService, "_delete_file"):
            await svc.delete_avatar(session, user)

        assert user.avatar_url is None

    async def test_delete_noop_when_no_avatar(self):
        """delete_avatar on a user with no avatar is a silent no-op."""
        user = UserFactory.build(avatar_url=None)
        session = AsyncMock()

        svc = ProfileService()
        # Should not raise
        with patch.object(ProfileService, "_delete_file") as mock_del:
            await svc.delete_avatar(session, user)
            mock_del.assert_not_called()

        assert user.avatar_url is None
