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

# --- the generator has to be worth running ---------------------------------
#
# TLP splits rows three ways, so a predicate that is never NULL leaves one
# arm empty and the relation degenerates into "the engine agrees with
# itself about TRUE and FALSE" -- which it will, forever, while printing a
# large and encouraging number. These are floors, not exact figures,
# because the generator is random; they exist so that a change which
# quietly makes it two-valued fails here instead of being discovered by a
# clean run that tested nothing.

import random
import sqlite3


def measure(n=400, null_rate=0.25, seed=7, rows=8, depth=2):
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    three = nulls = const = total = 0
    for _ in range(n):
        _, cols, rowvals = tlp.build(db, rnd, rows, null_rate)
        pred = tlp.gen_predicate(rnd, cols, depth)
        try:
            t, f, u = tlp.partition_sizes(db, pred)
        except sqlite3.Error:
            continue
        total += 1
        nulls += bool(u)
        three += bool(t and f and u)
        const += (max(t, f, u) == len(rowvals))
    return (100.0 * three / total, 100.0 * nulls / total, 100.0 * const / total)


three, nulls, const = measure()
ok("%.0f%% of predicates are genuinely three-valued" % three) if three >= 20 else \
    no("only %.0f%% of predicates are three-valued" % three,
       "below the 20%% floor: TLP's third partition is mostly empty")
ok("%.0f%% have a non-empty NULL partition" % nulls) if nulls >= 50 else \
    no("only %.0f%% have a NULL partition" % nulls)
ok("%.0f%% partition nothing, which is few enough" % const) if const <= 30 else \
    no("%.0f%% of predicates are constant across every row" % const)

# And the measurement itself has to respond, or the floors above are being
# met by a number that cannot move. With no NULLs in the data the only
# remaining source is a literal NULL inside the predicate, so the rate
# should fall a long way rather than to zero.
three0, _, _ = measure(null_rate=0.0)
ok("a NULL-free table drops it to %.0f%%, so the measure responds" % three0) \
    if three0 < three / 2 else \
    no("the vacuity measure does not respond to the NULL rate",
       "%.0f%% with NULLs, %.0f%% without" % (three, three0))

# Same seed, same programs. Without this a failing case cannot be replayed.
a = measure(n=50, seed=3)
b = measure(n=50, seed=3)
check("the generator is deterministic for a seed", a, b)

# --- the oracles can fail --------------------------------------------------
#
# Both report nothing against SQLite and always will. The number is worth
# what the evidence that they would report something else is worth, so each
# planted bug is asserted to be caught, and the two that only one oracle
# sees are asserted to be seen by exactly that one. That is the argument
# for carrying both.

def detection(kind, n=200, seed=0):
    rnd = random.Random(seed)
    real = sqlite3.connect(":memory:")
    hits = {"TLP": 0, "NoREC": 0}
    for _ in range(n):
        _, cols, _ = tlp.build(real, rnd, 8, 0.25)
        real.execute("CREATE INDEX IF NOT EXISTS i0 ON t(%s)" % cols[0][0])
        pred = tlp.gen_predicate(rnd, cols, 2)
        bent = tlp.Corrupt(real, kind)
        for name, fn in (("TLP", tlp.tlp_check), ("NoREC", tlp.norec_check)):
            try:
                if fn(bent, pred):
                    hits[name] += 1
            except sqlite3.Error:
                pass
    return hits["TLP"] * 100 // n, hits["NoREC"] * 100 // n


t_drop, n_drop = detection("drop-row")
ok("a dropped row is caught by TLP (%d%%)" % t_drop) if t_drop > 50 else \
    no("a dropped row is not reliably caught by TLP", "%d%%" % t_drop)
check("...and NoREC cannot see it, which is why both exist", n_drop, 0)

t_cnt, n_cnt = detection("count-off-by-one")
ok("a wrong indexed count is caught by NoREC (%d%%)" % n_cnt) if n_cnt > 50 else \
    no("a wrong indexed count is not caught by NoREC", "%d%%" % n_cnt)
check("...and TLP cannot see it, because TLP never asks for a count", t_cnt, 0)

# A bug that only fires for predicates mentioning NULL. This one is caught
# some of the time rather than always, which is the honest shape for a
# narrow bug and a random generator -- and the first version of it was
# caught 100% of the time because the condition tested the partition query
# rather than the predicate, and the partition query always says IS NULL.
t_narrow, _ = detection("null-only")
ok("a NULL-only bug is caught %d%% of the time" % t_narrow) if 5 <= t_narrow <= 80 else \
    no("a NULL-only bug is caught %d%% of the time" % t_narrow,
       "outside 5-80%%: either not narrow, or not being found at all")

# And the oracles must be quiet when nothing is wrong, or every number
# above is noise.
rnd = random.Random(11)
real = sqlite3.connect(":memory:")
false_alarms = 0
for _ in range(300):
    _, cols, _ = tlp.build(real, rnd, 8, 0.25)
    real.execute("CREATE INDEX IF NOT EXISTS i0 ON t(%s)" % cols[0][0])
    pred = tlp.gen_predicate(rnd, cols, 2)
    try:
        if tlp.tlp_check(real, pred) or tlp.norec_check(real, pred):
            false_alarms += 1
    except sqlite3.Error:
        pass
check("300 correct queries raise no false alarms", false_alarms, 0)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
