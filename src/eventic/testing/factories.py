"""Type-zoo models and deterministic builders for the canonicalization corpus.

Every supported Pydantic construct from ``ARCHITECTURE.md`` §3.2 appears here
with at least one valid instance. The property tested over this corpus is
``canonicalize(validate(canonicalize(x))) == canonicalize(x)``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    computed_field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 2


class WithComputed(BaseModel):
    a: int

    @computed_field
    @property
    def doubled(self) -> int:
        return self.a * 2


class NestedComputed(BaseModel):
    name: str
    child: WithComputed
    children: list[WithComputed]
    mapping: dict[str, WithComputed]

    @computed_field
    @property
    def total(self) -> int:
        return self.child.doubled + sum(c.doubled for c in self.children)


class DeepComputed(BaseModel):
    tag: str
    nested: NestedComputed
    depth_two: list[NestedComputed]
    depth_mapping: dict[str, NestedComputed]

    @computed_field
    @property
    def upper(self) -> str:
        return self.tag.upper()


class AllScalars(BaseModel):
    id: UUID
    aware: datetime
    naive: datetime
    day: date
    clock: time
    amount: Decimal
    color: Color
    priority: Priority
    data: bytes
    path: Path
    optional: str | None
    with_default: int = 42
    alias_field: Annotated[str, Field(alias="the_alias")] = "a"


class WithValidators(BaseModel):
    value: int

    @field_validator("value")
    @classmethod
    def _even(cls, v: int) -> int:
        if v % 2:
            raise ValueError("must be even")
        return v

    @model_validator(mode="after")
    def _positive(self) -> WithValidators:
        if self.value < 0:
            raise ValueError("must be non-negative")
        return self


class WithSerializers(BaseModel):
    amount: Decimal

    @field_serializer("amount")
    def _ser(self, v: Decimal) -> str:
        return f"{v:.2f}"

    @model_serializer(mode="wrap")
    def _wrap(self, handler: Any) -> Any:
        return handler(self)


class Square(BaseModel):
    kind: Literal["square"]
    side: float


class Circle(BaseModel):
    kind: Literal["circle"]
    radius: float


class Shape(BaseModel):
    shape: Annotated[Square | Circle, Field(discriminator="kind")]


class Container(BaseModel):
    items: list[WithComputed]
    tags: set[str]
    pair: tuple[int, str]
    mapping: dict[str, int]


class Empty(BaseModel):
    pass


class Recursive(BaseModel):
    label: str
    next: Recursive | None = None


@dataclass(frozen=True)
class ZooMember:
    name: str
    model: type[BaseModel]
    instances: tuple[BaseModel, ...]


def _instances(*xs: BaseModel) -> tuple[BaseModel, ...]:
    return xs


_TZ = timezone(timedelta(hours=2))


def _all_scalars_instances() -> tuple[BaseModel, ...]:
    return _instances(
        AllScalars(
            id=UUID("12345678-1234-5678-1234-567812345678"),
            aware=datetime(2024, 1, 2, 3, 4, 5, tzinfo=_TZ),
            naive=datetime(2024, 1, 2, 3, 4, 5),
            day=date(2024, 1, 2),
            clock=time(3, 4, 5),
            amount=Decimal("12.34"),
            color=Color.RED,
            priority=Priority.HIGH,
            data=b"\x00\x01",
            path=Path("/tmp/x"),
            optional=None,
        ),
        AllScalars(
            id=UUID("12345678-1234-5678-1234-567812345678"),
            aware=datetime(2024, 1, 2, 3, 4, 5, tzinfo=_TZ),
            naive=datetime(2024, 1, 2, 3, 4, 5),
            day=date(2024, 1, 2),
            clock=time(3, 4, 5),
            amount=Decimal("12.34"),
            color=Color.RED,
            priority=Priority.HIGH,
            data=b"\x00\x01",
            path=Path("/tmp/x"),
            optional="x",
            with_default=7,
            alias_field="b",
        ),
    )


def _shape_instances() -> tuple[BaseModel, ...]:
    return _instances(
        Shape(shape=Square(kind="square", side=2.0)),
        Shape(shape=Circle(kind="circle", radius=1.5)),
    )


ZOO: tuple[ZooMember, ...] = (
    ZooMember(
        "with_computed", WithComputed, _instances(WithComputed(a=1), WithComputed(a=-3))
    ),
    ZooMember(
        "nested_computed",
        NestedComputed,
        _instances(
            NestedComputed(
                name="n",
                child=WithComputed(a=1),
                children=[WithComputed(a=2)],
                mapping={"k": WithComputed(a=3)},
            )
        ),
    ),
    ZooMember(
        "deep_computed",
        DeepComputed,
        _instances(
            DeepComputed(
                tag="t",
                nested=NestedComputed(
                    name="n",
                    child=WithComputed(a=1),
                    children=[WithComputed(a=2)],
                    mapping={"k": WithComputed(a=3)},
                ),
                depth_two=[],
                depth_mapping={},
            )
        ),
    ),
    ZooMember("all_scalars", AllScalars, _all_scalars_instances()),
    ZooMember(
        "with_validators",
        WithValidators,
        _instances(WithValidators(value=2), WithValidators(value=0)),
    ),
    ZooMember(
        "with_serializers",
        WithSerializers,
        _instances(
            WithSerializers(amount=Decimal("1.5")),
            WithSerializers(amount=Decimal("0.00")),
        ),
    ),
    ZooMember("shape", Shape, _shape_instances()),
    ZooMember(
        "container",
        Container,
        _instances(
            Container(
                items=[WithComputed(a=1)],
                tags={"x", "y"},
                pair=(1, "a"),
                mapping={"k": 2},
            ),
            Container(items=[], tags=set(), pair=(0, ""), mapping={}),
        ),
    ),
    ZooMember("empty", Empty, _instances(Empty())),
    ZooMember(
        "recursive",
        Recursive,
        _instances(
            Recursive(label="a"), Recursive(label="a", next=Recursive(label="b"))
        ),
    ),
)


class WithSecret(BaseModel):
    token: SecretStr


class WithNestedSecret(BaseModel):
    inner: list[WithSecret]
