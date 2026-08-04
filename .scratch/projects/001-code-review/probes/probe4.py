from pydantic import BaseModel

class M(BaseModel):
    x: int = 1
    model_config = {"frozen": True, "extra": "allow"}
    def model_post_init(self, ctx):
        pass

m = M()
# Try private attribute set (Record.__setattr__ routes non-underscore elsewhere, but underscore goes to super().__setattr__)
try:
    m._foo = 1
    print("private set: OK")
except Exception as e:
    print("private set FAILED:", type(e).__name__, str(e)[:80])

# public set
try:
    m.y = 2
    print("public set: OK, extra =", m.__pydantic_extra__)
except Exception as e:
    print("public set FAILED:", type(e).__name__, str(e)[:80])
