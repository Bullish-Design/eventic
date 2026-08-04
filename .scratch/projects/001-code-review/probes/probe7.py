# Demonstrate local-vs-persisted divergence: validation coercion in new_obj vs raw object.__setattr__
import sys; sys.path.insert(0, 'src')
from pydantic import BaseModel, Field

class P(BaseModel):
    title: str | None = None
    model_config = {"frozen": True, "extra": "allow"}

# simulate Record.__setattr__ steps (minus store)
p = P(title="a")
data = p.model_dump(mode="python")
data["title"] = 5          # user assigns an int to a str field
data["version"] = 1
new_obj = P(**data)         # pydantic lax-mode coercion happens here
print("persisted/validated title:", repr(new_obj.title), type(new_obj.title).__name__)
# local reflection uses raw value:
object.__setattr__(p, "title", 5)
print("local title:", repr(p.title), type(p.title).__name__)
print("DIVERGED:", p.title != new_obj.title)
