"""Typed errors of the SSO protocol layer.

Every error is an :class:`~specivo.core.exceptions.AppError` whose ``code`` is
one of the error codes in the SSO design (``sso_idp_unreachable``,
``sso_token_invalid``, ...). The web callback turns the code into
``/login/?sso_error=<code>``; the API returns it in the usual error envelope.

``message`` is always generic and safe to show. The precise cause is in
``reason`` (a short machine-readable string for the ``specivo.sso`` log line)
and never contains tokens, codes, verifiers, secrets or IdP-supplied text.
"""

from __future__ import annotations

from enum import StrEnum

from specivo.core.exceptions import AppError


class TokenRejection(StrEnum):
    """Why an ID token, logout token or userinfo response was rejected."""

    MALFORMED = "malformed"
    WRONG_TYP = "wrong_typ"
    ALG_NOT_ALLOWED = "alg_not_allowed"
    UNKNOWN_KID = "unknown_kid"
    KEY_MISMATCH = "key_mismatch"
    BAD_SIGNATURE = "bad_signature"
    MISSING_CLAIM = "missing_claim"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    WRONG_AZP = "wrong_azp"
    EXPIRED = "expired"
    ISSUED_IN_FUTURE = "issued_in_future"
    TOO_OLD = "too_old"
    NOT_YET_VALID = "not_yet_valid"
    NONCE_MISMATCH = "nonce_mismatch"
    AT_HASH_MISMATCH = "at_hash_mismatch"
    MISSING_EVENTS = "missing_events"
    NONCE_PRESENT = "nonce_present"
    MISSING_SID_AND_SUB = "missing_sid_and_sub"
    SUB_MISMATCH = "sub_mismatch"
    USERINFO_REJECTED = "userinfo_rejected"
    TOKEN_RESPONSE_INVALID = "token_response_invalid"


class SsoError(AppError):
    """Base class of SSO protocol errors."""

    default_code = "sso_error"
    default_message = "Single sign-on failed"
    default_status = 400

    def __init__(self, reason: str, *, message: str | None = None) -> None:
        super().__init__(
            code=self.default_code,
            message=message or self.default_message,
            status_code=self.default_status,
        )
        #: Machine-readable cause, for logs only.
        self.reason = str(reason)
        #: The transaction's intent once it is known (``login`` or ``link``):
        #: a refused link returns to the profile, a refused login to the login page.
        self.intent = "login"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, reason={self.reason!r})"


class SsoIdpUnreachableError(SsoError):
    """Discovery, JWKS, token or userinfo endpoint timed out, failed or misbehaved."""

    default_code = "sso_idp_unreachable"
    default_message = "The identity provider is unavailable"
    default_status = 502


class SsoDiscoveryInvalidError(SsoError):
    """The discovery document or JWKS is unusable (issuer mismatch, http endpoint, ...)."""

    default_code = "sso_discovery_invalid"
    default_message = "The identity provider configuration is invalid"
    default_status = 502


class SsoTokenInvalidError(SsoError):
    """An ID token, logout token or userinfo response failed validation."""

    default_code = "sso_token_invalid"
    default_message = "The sign-in token is invalid"
    default_status = 401

    def __init__(self, reason: TokenRejection | str, *, detail: str | None = None) -> None:
        super().__init__(reason)
        #: Extra safe detail for the log (a claim name, a kid), never a token.
        self.detail = detail


class SsoUnknownSigningKeyError(SsoTokenInvalidError):
    """No published key has the token's ``kid``: the caller may refetch the JWKS once."""

    def __init__(self, kid: str | None) -> None:
        super().__init__(TokenRejection.UNKNOWN_KID, detail=f"kid={kid!r}")
        self.kid = kid


class SsoTokenExchangeFailedError(SsoError):
    """The token endpoint refused the authorization code (``invalid_grant`` etc.)."""

    default_code = "sso_token_exchange_failed"
    default_message = "The identity provider refused the sign-in"
    default_status = 400

    def __init__(self, reason: str, *, idp_error: str | None = None) -> None:
        super().__init__(reason)
        #: The OAuth ``error`` code from the IdP (sanitised), never its description.
        self.idp_error = idp_error


class SsoConfigurationError(SsoError):
    """The provider row cannot be used (unreadable or missing client secret, ...)."""

    default_code = "sso_provider_misconfigured"
    default_message = "Single sign-on is not configured correctly"
    default_status = 500


class SsoStateInvalidError(SsoError):
    """The login transaction is missing, expired, reused, from another browser or for another provider."""

    default_code = "sso_state_invalid"
    default_message = "The sign-in expired, try again"
    default_status = 400


class SsoUnavailableError(SsoError):
    """Single sign-on cannot run right now (no transaction store): fails closed."""

    default_code = "sso_unavailable"
    default_message = "Single sign-on is temporarily unavailable"
    default_status = 503


class SsoIdpRefusedError(SsoError):
    """The IdP answered the authorization request with ``error=`` (e.g. ``access_denied``)."""

    default_code = "sso_idp_error"
    default_message = "The identity provider refused the sign-in"
    default_status = 400


class SsoAccountNotProvisionedError(SsoError):
    """No Specivo account is linked to the external identity, and none may be created."""

    default_code = "sso_account_not_provisioned"
    default_message = "No account is connected to this identity"
    default_status = 403


class SsoAccountDisabledError(SsoError):
    """The linked account cannot sign in (deactivated, locked by an admin, service account)."""

    default_code = "sso_account_disabled"
    default_message = "The account is disabled"
    default_status = 403


class SsoEmailMissingError(SsoError):
    """JIT provisioning needs an email, and the identity provider sent none."""

    default_code = "sso_email_missing"
    default_message = "The identity provider did not send an email address"
    default_status = 403


class SsoEmailConflictError(SsoError):
    """JIT provisioning refused: another account already uses the email (never auto-linked)."""

    default_code = "sso_email_conflict"
    default_message = "An account with this email address already exists"
    default_status = 409


class SsoIdentityInUseError(SsoError):
    """A link was asked for an external identity that belongs to another user."""

    default_code = "sso_identity_in_use"
    default_message = "This identity is already connected to another account"
    default_status = 409


class SsoProviderAlreadyLinkedError(SsoError):
    """A link was asked at a provider where the user already has an identity."""

    default_code = "sso_provider_already_linked"
    default_message = "An account at this identity provider is already connected"
    default_status = 409
