import sys, types, uuid
# Simulate RecordMeta wrapping of staticmethod without importing eventic (dbos not needed for Queue? it is needed)
# Instead verify callable() filtering for various attrs:
class NS(dict): pass
ns = NS()
def make_class(name, ns):
    return type(name, (), ns)
# staticmethod object is callable -> wrapped
print("probe: staticmethod callable:", callable(staticmethod(lambda: 1)))
# property objects?
print("property callable:", callable(property()))
