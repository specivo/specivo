"""Profile service — avatar colors, photo upload, user preferences."""

import logging
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from specivo.core.config import get_settings
from specivo.core.exceptions import ValidationError
from specivo.models.user import User
from specivo.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

# Image magic bytes for validation
_MAGIC_BYTES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"RIFF": "image/webp",  # RIFF....WEBP
}

ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProfileService:
    """Service for user profile operations."""

    async def update_avatar_color(self, session: AsyncSession, user: User, color: str) -> None:
        """Update user's avatar color. Validates against the palette."""
        palette = await SettingsService().get_avatar_palette(session)
        if color not in palette:
            raise ValidationError(f"Invalid avatar color. Choose from: {', '.join(palette)}")
        user.preferences = {**user.preferences, "avatar_color": color}

    async def upload_avatar(
        self,
        session: AsyncSession,
        user: User,
        file_content: bytes,
        content_type: str,
        filename: str,
    ) -> str:
        """Upload and process avatar image. Returns the new avatar URL."""
        import io

        from PIL import Image

        settings = get_settings()
        max_bytes = settings.avatar_max_size_mb * 1024 * 1024
        max_dim = settings.avatar_max_dimension

        # Size check
        if len(file_content) > max_bytes:
            raise ValidationError(f"Avatar file too large. Maximum {settings.avatar_max_size_mb}MB.")

        # MIME type check
        if content_type not in ALLOWED_AVATAR_TYPES:
            raise ValidationError("Only JPEG, PNG, and WebP images are allowed.")

        # Magic bytes check
        if not self._verify_magic_bytes(file_content, content_type):
            raise ValidationError("File content does not match declared image type.")

        # Open with Pillow (catches non-image files)
        try:
            img = Image.open(io.BytesIO(file_content))
            img.verify()  # Verify it's a valid image
            # Re-open after verify (verify() leaves file in unusable state)
            img = Image.open(io.BytesIO(file_content))
        except Exception:
            raise ValidationError("Invalid image file.")

        # Strip EXIF metadata by re-encoding
        if img.mode in ("RGBA", "LA", "PA"):
            # WebP supports alpha
            pass
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize to fit within max dimensions
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        # Generate UUID filename with hash subdirectory
        file_uuid = uuid.uuid4().hex
        subdir = file_uuid[:2]
        rel_path = f"{subdir}/{file_uuid}.webp"

        # Save to disk
        avatar_dir = Path(settings.avatar_upload_dir) / subdir
        avatar_dir.mkdir(parents=True, exist_ok=True)
        abs_path = avatar_dir / f"{file_uuid}.webp"

        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=85)
        abs_path.write_bytes(buf.getvalue())

        # Delete old avatar if exists
        old_url = user.avatar_url
        if old_url:
            self._delete_file(old_url)

        # Update user
        avatar_url = f"/data/avatars/{rel_path}"
        user.avatar_url = avatar_url
        return avatar_url

    async def delete_avatar(self, session: AsyncSession, user: User) -> None:
        """Remove user's avatar photo."""
        if user.avatar_url:
            self._delete_file(user.avatar_url)
            user.avatar_url = None

    @staticmethod
    def _verify_magic_bytes(content: bytes, declared_type: str) -> bool:
        """Check that file magic bytes match the declared MIME type."""
        for magic, mime in _MAGIC_BYTES.items():
            if content[: len(magic)] == magic:
                if declared_type == "image/webp":
                    # WebP starts with RIFF, but need to check WEBP at offset 8
                    return len(content) > 11 and content[8:12] == b"WEBP"
                return mime == declared_type
        return False

    @staticmethod
    def _delete_file(avatar_url: str) -> None:
        """Delete an avatar file from disk."""
        settings = get_settings()
        # avatar_url is like "/data/avatars/ab/abcdef.webp"
        # Strip the "/data/avatars/" prefix to get the relative path
        prefix = "/data/avatars/"
        if avatar_url.startswith(prefix):
            rel = avatar_url[len(prefix) :]
            path = Path(settings.avatar_upload_dir) / rel
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Failed to delete avatar file: %s", path, exc_info=True)
