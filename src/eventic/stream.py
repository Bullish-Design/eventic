"""``Stream[T]`` — a declaration: a name, a state type, a schema version, and an
upcaster chain. An immutable value with no side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, RootModel, TypeAdapter

from eventic.canonical import build_exclude_map, contains_secret, model_fingerprint
from eventic.errors import ConfigError
from eventic.evolution import Upcaster, validate_chain
from eventic.ids import validate_stream_name

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, eq=False)
class Stream[T: BaseModel]:
    """A durable declaration for one aggregate stream.

    Equal and hashable by ``name`` — the name is the durable identity, so two
    declarations with the same name are the same stream even if the model
    differs.
    """

    model: type[T]
    name: str
    schema_version: int = 1
    upcasters: Mapping[int, Upcaster] = field(default_factory=dict[int, Upcaster])
    adapter: TypeAdapter[Any] = field(init=False, repr=False)
    exclude_map: Mapping[str, Any] = field(init=False, repr=False)
    fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Deliberate runtime guards at a public boundary: callers are not
        # statically typed, so the type system's guarantees do not hold here.
        if not isinstance(self.model, type) or not issubclass(self.model, BaseModel):  # type: ignore[reportUnnecessaryIsInstance, reportUnnecessaryIsinstance]
            raise ConfigError(
                "stream model must be a pydantic BaseModel subclass "
                f"(got {self.model!r})"
            )
        if issubclass(self.model, RootModel):  # type: ignore[reportUnnecessaryIsInstance]
            raise ConfigError(
                "RootModel streams are not supported; use a plain BaseModel"
            )
        object.__setattr__(self, "name", validate_stream_name(self.name))
        if not isinstance(self.schema_version, int) or self.schema_version < 1:  # type: ignore[reportUnnecessaryIsInstance]
            raise ConfigError("schema_version must be an int >= 1")
        if contains_secret(self.model):
            raise ConfigError(
                f"stream {self.name}: SecretStr fields are not supported "
                "(they serialize as '**********' and would be destroyed in an "
                "append-only log)"
            )
        validate_chain(
            self.upcasters,
            from_version=1,
            to_version=self.schema_version,
            subject=f"stream {self.name}",
        )
        object.__setattr__(self, "adapter", TypeAdapter(self.model))
        object.__setattr__(self, "exclude_map", build_exclude_map(self.model))
        object.__setattr__(self, "fingerprint", model_fingerprint(self.model))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Stream) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)
