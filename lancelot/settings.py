"""pydantic-settings DI provider (WBS U19). Lancelot does NOT depend on
pydantic-settings — the user brings their own BaseSettings; we only
provide the registration-time cached singleton. Missing required env
surfaces at BUILD (eager) so it is a boot crash, not a runtime 500."""
import functools


def settings_provider(settings_cls, *, eager: bool = False):
    @functools.lru_cache(maxsize=1)
    def _get():
        return settings_cls()  # pydantic-settings reads env here

    if eager:
        _get()  # instantiate now → missing required env crashes at boot
    return _get
