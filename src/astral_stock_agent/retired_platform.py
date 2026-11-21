"""Unconditional boundary for retired external-platform execution.

No environment flag, credential or caller opt-in can re-enable this integration.
Historical pure parsers/journals remain for read-only compatibility only.
"""

class RetiredPlatformError(ValueError):
    pass


def block():
    raise RetiredPlatformError('retired platform execution is disabled')
