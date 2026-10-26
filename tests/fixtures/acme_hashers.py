"""The example password hasher plugin from the guide, kept here so it is tested.

``docs/guide/install/password-hasher-plugins.md`` shows this module verbatim. A
hypothetical source system stores ``md5(salt + password)``; the plugin teaches
Specivo to verify it, wrapped in bcrypt. Change the two in step.
"""

import hashlib

from specivo.core.plugin import BasePluginConfig
from specivo.services.password_hashers import WrappedLegacyHasher


class AcmeTrackerMd5(WrappedLegacyHasher):
    algorithm = "acme_md5"

    def source_params(self, fields):  # import side: which source columns matter
        return {"salt": fields["salt"]}, fields["password_digest"]

    def inner_digest(self, password, params):  # login side: recompute the source's digest
        return hashlib.md5((params["salt"] + password).encode()).hexdigest()


class Plugin(BasePluginConfig):
    name, tier, version = "acme-hashers", "core", "1.0"

    def get_password_hashers(self):
        return [AcmeTrackerMd5()]
