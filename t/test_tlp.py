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
from collections import Counter
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

# --- the aggregate composition rules are load-bearing ----------------------
#
# Each aggregate recombines differently, and the rules look arbitrary until
# you try the wrong one. The right rule must raise no alarms against a
# correct database; every wrong rule must raise many. Without the second
# half, "0 disagreements" would be consistent with a rule that cannot
# disagree with anything.
#
# COUNT is the one worth staring at: its outer combiner is SUM, because
# counts ADD. Writing COUNT there by analogy with the others is the obvious
# mistake and it produces an almost totally broken tool.

def agg_alarms(agg, outer, n=250, seed=4):
    saved = tlp.AGGREGATES[agg]
    tlp.AGGREGATES[agg] = outer
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    bad = tested = 0
    try:
        for _ in range(n):
            _, cols, _ = tlp.build(db, rnd, 8, 0.25)
            ints = [c for c, ty in cols if ty == "INTEGER"]
            if not ints:
                continue
            pred = tlp.gen_predicate(rnd, cols, 2)
            try:
                tested += 1
                if tlp.agg_check(db, pred, agg, ints[0]):
                    bad += 1
            except sqlite3.Error:
                tested -= 1
    finally:
        tlp.AGGREGATES[agg] = saved
    return bad, tested


RIGHT = {"SUM": "SUM", "COUNT": "SUM", "MIN": "MIN", "MAX": "MAX"}
WRONG = {"SUM": ["MIN", "COUNT"], "COUNT": ["COUNT", "MIN"],
         "MIN": ["SUM", "MAX"], "MAX": ["SUM", "MIN"]}

for agg, right in sorted(RIGHT.items()):
    bad, tested = agg_alarms(agg, right)
    check("%s recombines with %s and raises no false alarms (%d checks)"
          % (agg, right, tested), bad, 0)
    for w in WRONG[agg]:
        bad, tested = agg_alarms(agg, w)
        ok("%s with the wrong combiner %s raises %d of %d" % (agg, w, bad, tested)) \
            if bad > tested // 3 else \
            no("%s with the wrong combiner %s raises only %d of %d" % (agg, w, bad, tested),
               "the rule is not doing any work, so the right one proves nothing")

# And the aggregate oracle sees a bug the other two cannot: an aggregate
# that is wrong while every row is present and every count agrees.
t_agg, n_agg = detection("aggregate-skew")
check("row-level TLP cannot see a skewed aggregate", t_agg, 0)
check("NoREC cannot see it either", n_agg, 0)

rnd = random.Random(5)
real = sqlite3.connect(":memory:")
caught = tried = 0
for _ in range(200):
    _, cols, _ = tlp.build(real, rnd, 8, 0.25)
    ints = [c for c, ty in cols if ty == "INTEGER"]
    if not ints:
        continue
    pred = tlp.gen_predicate(rnd, cols, 2)
    tried += 1
    try:
        if tlp.agg_check(tlp.Corrupt(real, "aggregate-skew"), pred, "SUM", ints[0]):
            caught += 1
    except sqlite3.Error:
        tried -= 1
pct = 100 * caught // tried if tried else 0
ok("the aggregate oracle catches it %d%% of the time" % pct) if pct > 50 else \
    no("the aggregate oracle catches it only %d%% of the time" % pct)

# --- join partitioning -----------------------------------------------------
#
# Two relations, and the first test for each is a hand-worked example
# rather than a random one. A relation that only ever meets generated data
# is a relation nobody has checked: if it were wrong, the generator would
# dutifully produce thousands of cases agreeing with it.

hand = sqlite3.connect(":memory:")
hand.executescript("""
CREATE TABLE t1 (a0 INTEGER, a1 TEXT);
CREATE TABLE t2 (b0 INTEGER, b1 TEXT);
INSERT INTO t1 VALUES (1,'x'),(2,NULL),(NULL,'y'),(3,'x');
INSERT INTO t2 VALUES (1,'x'),(2,'z'),(NULL,NULL);
""")
HAND_PRED = "t1.a0 = t2.b0 OR t1.a1 = t2.b1"

check("the inner relation holds on a worked example",
      tlp.join_check(hand, HAND_PRED), None)
check("the left-join relation holds on a worked example",
      tlp.leftjoin_check(hand, HAND_PRED), None)

# The arithmetic is small enough to do by hand, which is the point of
# having one case that is not generated. Three pairs are TRUE:
#
#   (1,'x') x (1,'x')     a0 = b0
#   (2,NULL) x (2,'z')    a0 = b0
#   (3,'x') x (1,'x')     a1 = b1
#
# Everything else is FALSE or NULL. (NULL,'y') matches nothing TRUE, so it
# is the one row the left join pads: 3 + 1 = 4.
#
# The first version of this said the join keeps 2, which is simply wrong --
# an expectation written from my head rather than worked out, and the kind
# of mistake that is the whole reason this project takes its numbers from
# an authority. SQLite said 3 and SQLite was right.
n = lambda q: hand.execute(q).fetchone()[0]
check("that example really is a 4x3 cross product", n("SELECT COUNT(*) FROM t1, t2"), 12)
check("...of which the join keeps 3",
      n("SELECT COUNT(*) FROM t1 JOIN t2 ON (%s)" % HAND_PRED), 3)
check("...and the left join yields 4, the 3 plus one padded row",
      n("SELECT COUNT(*) FROM t1 LEFT JOIN t2 ON (%s)" % HAND_PRED), 4)


def join_run(n=250, seed=2):
    """Random join predicates against a correct database."""
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    alarms = split = tested = 0
    for _ in range(n):
        t1, t2 = tlp.build2(db, rnd, 6, 0.25)
        pred = tlp.gen_join_predicate(rnd, t1, t2, 2)
        try:
            kept = db.execute("SELECT COUNT(*) FROM t1 JOIN t2 ON (%s)" % pred).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM t1, t2").fetchone()[0]
            bad = tlp.join_check(db, pred) or tlp.leftjoin_check(db, pred)
        except sqlite3.Error:
            continue
        tested += 1
        alarms += bool(bad)
        split += (0 < kept < total)
    return alarms, split, tested


alarms, split, tested = join_run()
check("%d correct join queries raise no false alarms" % tested, alarms, 0)
pct = 100 * split // tested
ok("%d%% of join predicates actually split the cross product" % pct) if pct >= 40 else \
    no("only %d%% of join predicates split the cross product" % pct,
       "the rest keep everything or nothing, so the ON clause is doing no work")

# Every predicate has to mention both tables, or it is a filter the planner
# pushes down and the join is not being tested at all.
rnd = random.Random(8)
db = sqlite3.connect(":memory:")
both = 0
for _ in range(200):
    t1, t2 = tlp.build2(db, rnd, 6, 0.25)
    pred = tlp.gen_join_predicate(rnd, t1, t2, 2)
    both += ("t1." in pred and "t2." in pred)
check("every generated ON predicate mentions both tables", both, 200)


def join_detection(kind, n=150, seed=0):
    rnd = random.Random(seed)
    real = sqlite3.connect(":memory:")
    hits = {"inner": 0, "left": 0}
    for _ in range(n):
        t1, t2 = tlp.build2(real, rnd, 6, 0.25)
        pred = tlp.gen_join_predicate(rnd, t1, t2, 2)
        bent = tlp.Corrupt(real, kind)
        for name, fn in (("inner", tlp.join_check), ("left", tlp.leftjoin_check)):
            try:
                if fn(bent, pred):
                    hits[name] += 1
            except sqlite3.Error:
                pass
    return hits["inner"] * 100 // n, hits["left"] * 100 // n


i_drop, l_drop = join_detection("join-drop-pair")
ok("a lost join pair is caught by the inner relation (%d%%)" % i_drop) if i_drop > 50 else \
    no("a lost join pair is not caught", "%d%%" % i_drop)
check("...and the left-join relation cannot see it", l_drop, 0)

i_pad, l_pad = join_detection("leftjoin-overpad")
ok("an over-padded outer join is caught by the left relation (%d%%)" % l_pad) if l_pad > 50 else \
    no("an over-padded outer join is not caught", "%d%%" % l_pad)
check("...and the inner relation never issues a LEFT JOIN, so it cannot", i_pad, 0)

# ---------------------------------------------------------------- the reducer
#
# A reducer has exactly one property that matters, and it is not how small
# the output is: whatever comes out MUST STILL FAIL. A reducer that shrinks
# past the bug produces a minimal case that reproduces nothing and sends
# whoever reads it hunting for a bug that is not there -- strictly worse
# than printing the unreduced version.
#
# Effectiveness is checkable here in a way it usually is not, because the
# bug was planted and its trigger condition is therefore known exactly:
#
#   drop-row        fires when the partition query returns more than 3 rows,
#                   and it returns one per table row, so 4 is the true floor
#   duplicate-row   fires for any non-empty result, so the floor is 1
#   null-only       fires above 2 rows, so the floor is 3
#
# Asserting the exact floor rather than "smaller than before" is what turns
# this from a smoke test into a measurement. "Smaller" is satisfied by a
# reducer that drops one row and gives up.

def find_and_reduce(kind, seed=3, rows=10, depth=3):
    """Search for a case against a planted bug, then shrink it."""
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    for _ in range(400):
        _, cols, data = tlp.build(db, rnd, rows, 0.25)
        tree = tlp.gen_tree(rnd, cols, depth)

        def fails(trial, pred, cols=cols):
            db.execute("DROP TABLE IF EXISTS t")
            db.execute("CREATE TABLE t (%s)" % ", ".join("%s %s" % c for c in cols))
            db.executemany("INSERT INTO t VALUES (%s)" % ",".join("?" * len(cols)),
                           trial)
            try:
                return tlp.tlp_check(tlp.Corrupt(db, kind), pred) is not None
            except sqlite3.Error:
                return False

        if not fails(data, tlp.render(tree)):
            continue
        before = (len(data), tlp.tree_size(tree))
        small, stree = tlp.reduce_case(fails, cols, list(data), tree)
        return before, small, stree, cols, fails
    return None


FLOORS = {"drop-row": 4, "duplicate-row": 1, "null-only": 3}
for kind, floor in FLOORS.items():
    got = find_and_reduce(kind)
    if got is None:
        no("found a case to reduce for %s" % kind)
        continue
    before, small, stree, cols, fails = got
    # Soundness. The only property that matters.
    check("%s: the reduced case still fails" % kind,
          fails(small, tlp.render(stree)), True)
    # Effectiveness, against the planted bug's known floor.
    check("%s: shrinks to its true minimum of %d rows" % (kind, floor),
          len(small), floor)
    ok("%s: predicate %d nodes -> %d, rows %d -> %d"
       % (kind, before[1], tlp.tree_size(stree), before[0], len(small)))
    # The report has to be runnable, which means real SQL and not a repr.
    probe = sqlite3.connect(":memory:")
    ddl = "CREATE TABLE t (%s)" % ", ".join("%s %s" % c for c in cols)
    probe.execute(ddl)
    for r in small:
        probe.execute("INSERT INTO t VALUES (%s)"
                      % ", ".join(tlp.sql_literal(v) for v in r))
    # Counter, not sorted(): a row is a tuple mixing NULL with integers and
    # sorted() raises on that comparison. It is also the right relation --
    # these are multisets, and SQL does not promise an order.
    check("%s: the printed INSERTs round-trip through sqlite3" % kind,
          Counter(probe.execute("SELECT * FROM t").fetchall()), Counter(small))

# Reducing something that does not fail is a caller error, not a no-op: it
# would return a "minimal case" for a bug that was never there.
try:
    tlp.reduce_case(lambda rows, pred: False, [("c0", "INTEGER")],
                    [(1,)], ("atom", "c0 = 1"))
    no("reduce_case refuses a case that does not fail")
except ValueError:
    ok("reduce_case refuses a case that does not fail")

# The shrink order must be WELL-FOUNDED, which is the property whose absence
# made the first version of this loop run forever: it offered NULL for 0 and
# 0 for NULL, each counting as progress. So no candidate may lead back to
# the value it replaced.
cycles = []
for ty, vals in (("INTEGER", (None, 0, 3, -1)), ("TEXT", (None, "", "aa"))):
    for v in vals:
        for cand in tlp.simpler_than(v, ty):
            if v in tlp.simpler_than(cand, ty):
                cycles.append((ty, v, cand))
check("the value shrink order admits no cycle", cycles, [])
check("NULL is the bottom of the order", tlp.simpler_than(None, "INTEGER"), ())
check("0 shrinks only to NULL", tlp.simpler_than(7, "INTEGER"), (None, 0))

# render and gen_tree have to be the same generator the other oracles use,
# or the reducer is shrinking predicates nothing else would ever produce.
rnd = random.Random(11)
db = sqlite3.connect(":memory:")
_, cols, _ = tlp.build(db, rnd, 6, 0.25)
sizes = []
for _ in range(200):
    tree = tlp.gen_tree(rnd, cols, 2)
    sizes.append(tlp.tree_size(tree))
    if tlp.tree_size(tree) != 1 and not tlp.subtrees(tree):
        no("a compound predicate reports no subtrees", tlp.render(tree))
        break
else:
    ok("every compound predicate offers a subtree to shrink to (max %d nodes)"
       % max(sizes))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
