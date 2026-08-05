"""``Meta[M]`` — typed, versioned metadata, using the same machinery as stream
state: canonicalization, schema version, and an upcaster chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter

from eventic.canonical import build_exclude_map, contains_secret, model_fingerprint
from eventic.errors import ConfigError
from eventic.evolution import Upcaster, validate_chain

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True, eq=False)
class Meta[M: BaseModel]:
    """A durable declaration for one metadata type."""

    model: type[M]
    version: int = 1
    upcasters: Mapping[int, Upcaster] = field(default_factory=dict)
    adapter: TypeAdapter[Any] = field(init=False, repr=False)
    exclude_map: Mapping[str, Any] = field(init=False, repr=False)
    fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not (isinstance(self.model, type) and issubclass(self.model, BaseModel)):
            raise ConfigError(
                f"meta model must be a pydantic BaseModel subclass (got {self.model!r})"
            )
        if not isinstance(self.version, int) or self.version < 1:
            raise ConfigError("meta version must be an int >= 1")
        if contains_secret(self.model):
            raise ConfigError("SecretStr fields are not supported in meta")
        validate_chain(
            self.upcasters,
            from_version=1,
            to_version=self.version,
            subject="meta",
        )
        object.__setattr__(self, "adapter", TypeAdapter(self.model))
        object.__setattr__(self, "exclude_map", build_exclude_map(self.model))
        object.__setattr__(self, "fingerprint", model_fingerprint(self.model))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Meta) and self.model is other.model

    def __hash__(self) -> int:
        return hash(self.model)


class _Empty(BaseModel):
    """The no-metadata payload; serializes to ``{}``."""


NoMeta = Meta[_Empty](model=_Empty)
