"""pydantic is the repo's model standard.

Every structured value crossing a boundary (baseline YAML, sqlcmd rows)
is a frozen pydantic model validated at parse time — unknown fields are
rejected, not silently carried. The one exception is config.Instance, a
pydantic-settings BaseSettings (I.cfg) — still frozen and validated, but
extra-tolerant, since the environment legitimately carries non-config
ACU_* vars.
"""

from pydantic import BaseModel, ConfigDict, ValidationError


class Model(BaseModel):
    """Base for all acu models: immutable, unknown fields are errors."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def validation_summary(exc: ValidationError) -> str:
    """One line per error: dotted field path + pydantic's message."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )
