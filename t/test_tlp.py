#!/usr/bin/env python3
"""Tests for tlp.

Two halves, and the second is the one that matters.

The first checks that SQLite agrees with the standard's three-valued truth
tables. It passed on its first run and would have passed if the tables had
been copied out wrong in a way SQLite happens to share.

So the second half plants errors in those tables and asserts the check goes
red. A conformance test that has never been watched to fail is a test that
reports the conformance of nothing.

    python3 t/test_tlp.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tlp  # noqa: E402

passed = 0
failed = 0


def ok(label):
    global passed
    passed += 1
    print("ok   %s" % label)


def no(label, detail=""):
    global failed
    failed += 1
    print("FAIL %s" % label)
    if detail:
        print("       %s" % detail)


def check(label, got, want):
    if got == want:
        ok(label)
    else:
        no(label, "got %r, want %r" % (got, want))


def disagreements(depth):
    """How many expressions SQLite and the model disagree on."""
    import sqlite3
    db = sqlite3.connect(":memory:")
    n = 0
    for sql, want in tlp.enumerate_exprs(depth):
        if tlp.sql_value(db, sql) is not want:
            n += 1
    return n


# --- the three-valued model matches SQLite ---------------------------------

check("depth 1 agrees with SQLite", disagreements(1), 0)
check("depth 2 agrees with SQLite", disagreements(2), 0)

# The enumeration has to actually grow, or "depth 2" is a label on the same
# 78 expressions and the number above means nothing.
n1 = len(tlp.enumerate_exprs(1))
n2 = len(tlp.enumerate_exprs(2))
check("depth 1 enumerates 78 expressions", n1, 78)
ok("depth 2 enumerates %d, which is more than %d" % (n2, n1)) if n2 > n1 * 10 else \
    no("depth 2 barely grows", "%d vs %d" % (n2, n1))

# All three truth values have to appear, or the exercise is two-valued and
# the whole project rests on nothing.
vals = {v for _, v in tlp.enumerate_exprs(1)}
check("all three truth values occur", vals, {True, False, None})


# --- the check can fail ----------------------------------------------------
#
# Each of these is a wrong truth table. The suite asserts the conformance
# check notices, because otherwise it is reporting agreement with a model
# nobody has tested.

def with_table(name, fn, depth=1):
    """Swap one operator for a broken one and count the disagreements."""
    original = getattr(tlp, name, None)
    if original is None:
        original = tlp.BINARY_OPS[name]
        tlp.BINARY_OPS[name] = fn
    else:
        setattr(tlp, name, fn)
        # The operator table captured the original at import time.
        for k, v in list(tlp.BINARY_OPS.items()):
            if v is original:
                tlp.BINARY_OPS[k] = fn
    try:
        return disagreements(depth)
    finally:
        if hasattr(tlp, name) and getattr(tlp, name) is fn:
            setattr(tlp, name, original)
        for k, v in list(tlp.BINARY_OPS.items()):
            if v is fn:
                tlp.BINARY_OPS[k] = original


# AND where FALSE does not dominate: "FALSE AND NULL" becomes NULL.
n = with_table("t_and", lambda a, b: None if (a is None or b is None)
               else (a and b))
ok("a wrong AND table is caught (%d disagreements)" % n) if n else \
    no("a wrong AND table is NOT caught")

# OR where TRUE does not dominate.
n = with_table("t_or", lambda a, b: None if (a is None or b is None)
               else (a or b))
ok("a wrong OR table is caught (%d disagreements)" % n) if n else \
    no("a wrong OR table is NOT caught")

# NOT NULL becoming FALSE rather than NULL.
n = with_table("t_not", lambda a: False if a is None else (not a))
ok("a wrong NOT table is caught (%d disagreements)" % n) if n else \
    no("a wrong NOT table is NOT caught")

# = that does not propagate NULL, which is the classic mistake.
n = with_table("t_eq", lambda a, b: a == b)
ok("an = that does not propagate NULL is caught (%d)" % n) if n else \
    no("an = that does not propagate NULL is NOT caught")

# One deliberate non-case, recorded rather than chased. Rewriting t_is from
# `a is b` to `a == b` changes nothing, because on the domain {True, False,
# None} Python's two operators agree everywhere. It is an equivalent mutant,
# not a gap in the suite, and the distinction is worth keeping visible.
n = with_table("t_is", lambda a, b: a == b)
check("`a is b` and `a == b` are equivalent here, so this survives", n, 0)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
