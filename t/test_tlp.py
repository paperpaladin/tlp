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

# -------------------------------------------- DISTINCT and GROUP BY
#
# These two need OPPOSITE composition rules, and each one is a false-alarm
# generator in the other's place -- which is the same trap as the aggregate
# table and the reason both are measured rather than asserted.
#
# DISTINCT recombines with UNION, a set union, because a value can appear
# in two partitions: two rows sharing a value can land on opposite sides of
# the predicate. GROUP BY must recombine with UNION ALL inside, because the
# three partial counts for one group are the entire point and a set union
# would collapse two arms that happened to count the same number of rows.

def false_alarms(fn, n=200, seed=0, **kw):
    """How often a relation accuses a database that is behaving correctly."""
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    alarms = tested = 0
    for _ in range(n):
        _, cols, _ = tlp.build(db, rnd, 8, 0.25)
        pred = tlp.gen_predicate(rnd, cols, 2)
        try:
            bad = fn(db, pred, cols[0][0], **kw)
        except sqlite3.Error:
            continue
        tested += 1
        alarms += bool(bad)
    return alarms, tested

# The correct rules must never accuse a correct engine. This is the half
# that makes the tool usable at all: every report costs a human reading a
# schema and a query.
#
# Called with NO keyword, so the DEFAULT is what is under test. Passing
# combine="UNION" explicitly -- which is what the first version of this did
# -- tests a configuration no caller uses and leaves the shipped default
# unchecked: changing the default to UNION ALL kept both of these green.
# A parameterised test that always supplies the parameter has stopped
# testing the thing that ships.
check("the DISTINCT relation as shipped raises no false alarm",
      false_alarms(tlp.distinct_check)[0], 0)
check("the GROUP BY relation as shipped raises no false alarm",
      false_alarms(tlp.group_check)[0], 0)
check("...and the default composition rules are the correct ones",
      (tlp.distinct_check.__defaults__, tlp.group_check.__defaults__),
      (("UNION",), ("SUM",)))

# And every wrong rule must raise them in quantity, or the right rule was
# not load-bearing and the relation is not testing what it claims.
# Floors as a FRACTION of what was tested, not a count: written as counts
# they were sized for a 300-case sweep and silently became unreachable when
# this runs 200, which failed loudly here but would elsewhere have been a
# test that could never pass.
MEASURED = {}   # table row label -> the count this run actually measured

for combine, floor in (("UNION ALL", 0.4), ("INTERSECT", 0.9), ("EXCEPT", 0.9)):
    a, t = false_alarms(tlp.distinct_check, combine=combine)
    MEASURED["`%s`" % combine] = a
    if a >= floor * t:
        ok("DISTINCT recombined with %s raises %d false alarms in %d"
           % (combine, a, t))
    else:
        no("DISTINCT recombined with %s is not load-bearing" % combine,
           "%d alarms in %d" % (a, t))

for outer, floor in (("COUNT", 0.9), ("MIN", 0.4), ("MAX", 0.4), ("AVG", 0.4)):
    a, t = false_alarms(tlp.group_check, outer=outer)
    MEASURED["`%s`" % outer] = a
    if a >= floor * t:
        ok("GROUP BY counts recombined with %s raise %d false alarms in %d"
           % (outer, a, t))
    else:
        no("GROUP BY counts recombined with %s is not load-bearing" % outer,
           "%d alarms in %d" % (a, t))

# UNION ALL for DISTINCT, and MIN/MAX/AVG for GROUP BY, all raise the SAME
# number of false alarms -- and it is not a coincidence. Both fire exactly
# when one value of the column lands in more than one partition: UNION ALL
# then keeps it twice, and the per-arm counts then sum to more than any one
# of them. The same event, seen from two sides.
rnd = random.Random(0)
db = sqlite3.connect(":memory:")
spanning = 0
for _ in range(200):
    _, cols, _ = tlp.build(db, rnd, 8, 0.25)
    pred = tlp.gen_predicate(rnd, cols, 2)
    col = cols[0][0]
    try:
        arms = db.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT DISTINCT %s FROM t WHERE (%s)"
            "  UNION ALL SELECT DISTINCT %s FROM t WHERE NOT (%s)"
            "  UNION ALL SELECT DISTINCT %s FROM t WHERE (%s) IS NULL)"
            % (col, pred, col, pred, col, pred)).fetchone()[0]
        whole = db.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT %s FROM t)" % col).fetchone()[0]
    except sqlite3.Error:
        continue
    spanning += (arms > whole)
check("the UNION ALL false alarms are exactly the values that span partitions",
      false_alarms(tlp.distinct_check, combine="UNION ALL")[0], spanning)
check("...and so are the MIN ones",
      false_alarms(tlp.group_check, outer="MIN")[0], spanning)

# The DISTINCT relation leans on three operators agreeing about NULL:
# DISTINCT, UNION and GROUP BY all treat two NULLs as the same value, while
# `=` says NULL = NULL is NULL. If UNION used the `=` rule, a NULL coming
# from two arms would survive twice and the relation would be false for
# every table containing one. That assumption is cheap to check directly.
probe = sqlite3.connect(":memory:")
check("UNION treats two NULLs as the same value",
      probe.execute("SELECT NULL UNION SELECT NULL").fetchall(), [(None,)])
check("...and `=` does not",
      probe.execute("SELECT NULL = NULL").fetchone()[0], None)
check("...and GROUP BY agrees with UNION, not with `=`",
      probe.execute("SELECT COUNT(*) FROM "
                    "(SELECT NULL AS g UNION ALL SELECT NULL) GROUP BY g"
                    ).fetchall(), [(2,)])


def single_table_detection(kind, n=120, seed=0):
    """Which of the five single-table relations notices a planted bug."""
    rnd = random.Random(seed)
    real = sqlite3.connect(":memory:")
    hits = dict.fromkeys(("TLP", "NoREC", "agg", "DISTINCT", "GROUP"), 0)
    for _ in range(n):
        _, cols, _ = tlp.build(real, rnd, 8, 0.25)
        pred = tlp.gen_predicate(rnd, cols, 2)
        bent = tlp.Corrupt(real, kind)
        ints = [c for c, ty in cols if ty == "INTEGER"]
        for name, fn in (("TLP", tlp.tlp_check), ("NoREC", tlp.norec_check)):
            try:
                hits[name] += bool(fn(bent, pred))
            except sqlite3.Error:
                pass
        for name, fn in (("DISTINCT", tlp.distinct_check), ("GROUP", tlp.group_check)):
            try:
                hits[name] += bool(fn(bent, pred, cols[0][0]))
            except sqlite3.Error:
                pass
        if ints:
            try:
                hits["agg"] += bool(tlp.agg_check(bent, pred, "SUM", ints[0]))
            except sqlite3.Error:
                pass
    return {k: v * 100 // n for k, v in hits.items()}


# Each new relation catches exactly the class it was built for. These two
# bugs leave every row present, every count right and every whole-table
# aggregate right, so nothing that does not write DISTINCT or GROUP BY can
# see them -- which is the argument for carrying five relations.
d = single_table_detection("distinct-dup")
ok("a duplicate in a DISTINCT result is caught by the DISTINCT relation (%d%%)"
   % d["DISTINCT"]) if d["DISTINCT"] > 50 else \
    no("a duplicate in a DISTINCT result is not caught", repr(d))
check("...and no other relation writes DISTINCT, so none can see it",
      [d[k] for k in ("TLP", "NoREC", "agg", "GROUP")], [0, 0, 0, 0])

g = single_table_detection("group-skew")
ok("one group counted wrong is caught by the GROUP BY relation (%d%%)"
   % g["GROUP"]) if g["GROUP"] > 50 else \
    no("one group counted wrong is not caught", repr(g))
check("...and nothing else groups, so nothing else can see it",
      [g[k] for k in ("TLP", "NoREC", "agg", "DISTINCT")], [0, 0, 0, 0])

# The DISTINCT relation reads 0% against `drop-row`, and the tempting
# reading is that it cannot see a lost row. It is not what is measured:
# `drop-row` fires on "UNION ALL" and this relation recombines with
# "UNION". Widening the bug to any set operation separates the two
# explanations -- and the column moves off zero, so the relation sees lost
# rows fine and it was the bug that could not see the relation.
narrow = single_table_detection("drop-row")
wide = single_table_detection("setop-drop-row")
check("a UNION-ALL-only lost row is invisible to the DISTINCT relation",
      narrow["DISTINCT"], 0)
ok("...because of the trigger, not the relation: widened, it is caught (%d%%)"
   % wide["DISTINCT"]) if wide["DISTINCT"] > 50 else \
    no("the DISTINCT relation cannot see a lost row even when widened",
       repr(wide))

# ------------------------------------------------------------- HAVING
#
# HAVING partitions GROUPS, not rows, and the groups are formed by the
# GROUP BY before HAVING ever runs. So each group arrives whole in exactly
# one arm, already counted, and the recombination is a plain UNION ALL with
# no re-aggregation -- the simplest rule here, in the exact place where
# item 09 needed the most care.
#
# Which moves what is load-bearing. For DISTINCT and GROUP BY the
# composition OPERATOR was the trap. Here it is not a trap at all, and the
# traps are all in the arms. Both halves of that are measured below.

def having_alarms(n=200, seed=0, **kw):
    """False alarms from the HAVING relation against a correct engine."""
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    a = t = 0
    for _ in range(n):
        _, cols, _ = tlp.build(db, rnd, 8, 0.40)
        pred = tlp.gen_having(rnd, cols, 2)
        try:
            bad = tlp.having_check(db, pred, cols[0][0], **kw)
        except sqlite3.Error:
            continue
        t += 1
        a += bool(bad)
    return a, t

# As shipped, with no keyword, so the defaults are what is under test.
check("the HAVING relation as shipped raises no false alarm",
      having_alarms()[0], 0)
# Spelled out literally rather than as tlp.HAVING_ARMS, because the
# constant IS the thing under test: comparing it to itself passes for any
# value it is given. Changing the shipped arms to a two-way split left this
# green when it was written the other way.
check("...and its default arms and combiner are the correct ones",
      tlp.having_check.__defaults__,
      ("UNION ALL", ("(%s)", "NOT (%s)", "(%s) IS NULL")))

# The arms are load-bearing: they must cover every group exactly once.
A = tlp.HAVING_ARMS
for label, kw, floor in (
        ("dropping the IS NULL arm", {"arms": A[:2]}, 0.3),
        ("IS NOT TRUE alongside an IS NULL arm",
         {"arms": ("(%s)", "(%s) IS NOT TRUE", "(%s) IS NULL")}, 0.3),
        ("a duplicated TRUE arm",
         {"arms": ("(%s)", "(%s)", "NOT (%s)", "(%s) IS NULL")}, 0.6)):
    a, t = having_alarms(**kw)
    MEASURED[label] = a
    if a >= floor * t:
        ok("HAVING: %s raises %d false alarms in %d" % (label, a, t))
    else:
        no("HAVING: %s is not load-bearing" % label, "%d in %d" % (a, t))

# And the combiner is NOT load-bearing, which is the opposite of every
# other relation here and is recorded rather than quietly relied on. Each
# arm emits at most one row per group key and the key is unique within a
# group, so no two rows of the recombined query are ever identical and the
# set union has nothing to collapse.
check("HAVING: UNION for UNION ALL is an EQUIVALENT rewrite, not a bug",
      having_alarms(combine="UNION")[0], 0)

# A two-arm split is also sound: `p IS NOT TRUE` is exactly FALSE-or-NULL,
# so p and p IS NOT TRUE already cover every group once. The three-way
# split is therefore a choice, not a correctness requirement -- and the
# same holds for the row-level relation this whole project is named after.
check("HAVING: a two-arm IS NOT TRUE split is sound too",
      having_alarms(arms=("(%s)", "(%s) IS NOT TRUE"))[0], 0)

rnd = random.Random(0)
db = sqlite3.connect(":memory:")
two = three = tested = 0
for _ in range(200):
    _, cols, _ = tlp.build(db, rnd, 8, 0.25)
    pred = tlp.gen_predicate(rnd, cols, 2)
    try:
        whole = tlp.rows_of(db, "SELECT * FROM t")
        p3 = tlp.rows_of(db, "SELECT * FROM t WHERE (%s) "
                             "UNION ALL SELECT * FROM t WHERE NOT (%s) "
                             "UNION ALL SELECT * FROM t WHERE (%s) IS NULL"
                             % (pred, pred, pred))
        p2 = tlp.rows_of(db, "SELECT * FROM t WHERE (%s) "
                             "UNION ALL SELECT * FROM t WHERE (%s) IS NOT TRUE"
                             % (pred, pred))
    except sqlite3.Error:
        continue
    tested += 1
    three += (whole != p3)
    two += (whole != p2)
check("row-level TLP: the three-arm split holds", three, 0)
check("row-level TLP: a two-arm IS NOT TRUE split holds just as well", two, 0)

# The generator has to produce legal HAVING SQL, and a HAVING partition has
# to actually split the groups. The floor is much lower than the row-level
# 32% for a structural reason, measured here rather than excused: there are
# only about four groups in an eight-row table, and splitting four things
# three ways while leaving no arm empty is simply harder than splitting
# eight.
rnd = random.Random(0)
db = sqlite3.connect(":memory:")
split = tested = errors = 0
groups = []
for _ in range(400):
    _, cols, _ = tlp.build(db, rnd, 8, 0.40)
    col = cols[0][0]
    pred = tlp.gen_having(rnd, cols, 2)
    try:
        t, f, u = tlp.having_group_sizes(db, pred, col)
        groups.append(db.execute(
            "SELECT COUNT(*) FROM (SELECT %s FROM t GROUP BY %s)"
            % (col, col)).fetchone()[0])
    except sqlite3.Error:
        errors += 1
        continue
    tested += 1
    split += bool(t and f and u)
check("every generated HAVING predicate is legal SQL", errors, 0)
pct = split * 100 // tested
ok("HAVING predicates split the groups three ways %d%% of the time "
   "(%.1f groups per table)" % (pct, sum(groups) / len(groups))) if pct >= 12 \
    else no("HAVING predicates almost never split the groups", "%d%%" % pct)


def having_detection(kind, n=120, seed=0):
    rnd = random.Random(seed)
    real = sqlite3.connect(":memory:")
    hits = dict.fromkeys(("TLP", "NoREC", "agg", "DISTINCT", "GROUP",
                          "HAVING"), 0)
    for _ in range(n):
        _, cols, _ = tlp.build(real, rnd, 8, 0.40)
        col = cols[0][0]
        bent = tlp.Corrupt(real, kind)
        pred = tlp.gen_predicate(rnd, cols, 2)
        hv = tlp.gen_having(rnd, cols, 2)
        ints = [c for c, ty in cols if ty == "INTEGER"]
        probes = [("TLP", lambda: tlp.tlp_check(bent, pred)),
                  ("NoREC", lambda: tlp.norec_check(bent, pred)),
                  ("DISTINCT", lambda: tlp.distinct_check(bent, pred, col)),
                  ("GROUP", lambda: tlp.group_check(bent, pred, col)),
                  ("HAVING", lambda: tlp.having_check(bent, hv, col))]
        if ints:
            probes.append(("agg", lambda: tlp.agg_check(bent, pred, "SUM", ints[0])))
        for name, fn in probes:
            try:
                hits[name] += bool(fn())
            except sqlite3.Error:
                pass
    return {k: v * 100 // n for k, v in hits.items()}


# A group wrongly excluded by a HAVING filter. Nothing else here writes a
# HAVING clause, so nothing else can see it -- the sixth relation earns its
# place the same way the other five did.
h = having_detection("having-drop-group")
ok("a group lost to a HAVING filter is caught by the HAVING relation (%d%%)"
   % h["HAVING"]) if h["HAVING"] > 50 else \
    no("a group lost to a HAVING filter is not caught", repr(h))
check("...and nothing else writes HAVING, so nothing else can see it",
      [h[k] for k in ("TLP", "NoREC", "agg", "DISTINCT", "GROUP")],
      [0, 0, 0, 0, 0])

# ------------------------------------------- the README against this run
#
# Every false-alarm count in README.md is a number somebody copied out of a
# terminal, which means it can drift away from what the code does while the
# suite stays green -- the exact failure the portfolio generator exists to
# prevent for the top-level document, and which nothing was preventing here.
# This repo's own README had already overstated itself once: it claimed a
# test count of 48 after the suite had grown to 65.
#
# So the tables are checked against the numbers measured a few lines above,
# in this process, on this machine.

readme = (ROOT / "README.md").read_text()

# Matched as WHOLE ROWS built from the measured numbers, not by looking for
# a label and a number somewhere near each other. `MIN` and `MAX` head rows
# in the aggregate table as well as appearing in the DISTINCT/GROUP one, and
# the aggregate table happens to contain the value 127 -- so a loose match
# lets a number from one table satisfy a claim about another. Constructing
# the row and requiring it verbatim cannot do that.
expected = [
    "| `UNION ALL` | %d | | `COUNT` | %d |"
    % (MEASURED["`UNION ALL`"], MEASURED["`COUNT`"]),
    "| `INTERSECT` | %d | | `MIN` | %d |"
    % (MEASURED["`INTERSECT`"], MEASURED["`MIN`"]),
    "| `EXCEPT` | %d | | `MAX` | %d |"
    % (MEASURED["`EXCEPT`"], MEASURED["`MAX`"]),
    "| | | | `AVG` | %d |" % MEASURED["`AVG`"],
    "| drop the `IS NULL` arm | %d |"
    % MEASURED["dropping the IS NULL arm"],
    "| `IS NOT TRUE` alongside an `IS NULL` arm | %d |"
    % MEASURED["IS NOT TRUE alongside an IS NULL arm"],
    "| a duplicated TRUE arm | %d |" % MEASURED["a duplicated TRUE arm"],
]
check("every false-alarm count in README.md is the one measured here",
      [row for row in expected if row not in readme], [])

# The suite's own size is deliberately NOT checked here. Counting this
# suite's checks from inside this suite makes the expected value depend on
# how many checks do the counting, so every check added later has to be
# matched by editing an arithmetic constant in one of them. That is a test
# that breaks for reasons unrelated to the code, which is worse than the
# drift it would catch. The README's "N passed" line is verified by reading
# it against a run, the same way every other transcript in it is.

# --------------------------------------- three tables, and join reordering
#
# Two tables give a planner one join and two orders. Three give it a real
# choice, and reordering is where it has the most room to be wrong.
#
# The reordering relation rests entirely on one documented SQLite
# behaviour: CROSS JOIN suppresses reordering, so the tables are visited in
# the order written. If that were not true the relation would be comparing
# a query against itself, so it is checked directly rather than taken from
# the documentation.

def plan_tables(db, sql):
    """The order EXPLAIN QUERY PLAN says the tables are visited in."""
    order = []
    for row in db.execute("EXPLAIN QUERY PLAN " + sql):
        for word in row[3].split():
            if word in ("t1", "t2", "t3") and word not in order:
                order.append(word)
    return order


rnd = random.Random(0)
db = sqlite3.connect(":memory:")
forced_wrong = free_differs = tested = 0
for _ in range(150):
    specs = tlp.build3(db, rnd, 6, 0.25)
    pred = tlp.gen_join3_predicate(rnd, specs, 2)
    try:
        forced = plan_tables(
            db, "SELECT * FROM t1 CROSS JOIN t2 CROSS JOIN t3 WHERE %s" % pred)
        free = plan_tables(
            db, "SELECT * FROM t1 JOIN t2 JOIN t3 WHERE %s" % pred)
    except sqlite3.Error:
        continue
    tested += 1
    if forced != ["t1", "t2", "t3"]:
        forced_wrong += 1
    free_differs += (free != forced)
check("CROSS JOIN pins the join order to the order written", forced_wrong, 0)
pct = free_differs * 100 // tested
ok("...and the planner picks a different order %d%% of the time, so the "
   "relation is not comparing a query to itself" % pct) if pct >= 25 else \
    no("the planner almost never reorders, so the relation is near-vacuous",
       "%d%%" % pct)


def join3_alarms(fn, n=150, seed=0, **kw):
    """False alarms from a three-table relation against a correct engine."""
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    a = t = 0
    for _ in range(n):
        specs = tlp.build3(db, rnd, 6, 0.25, **kw)
        pred = tlp.gen_join3_predicate(rnd, specs, 2)
        p12 = "%s.%s = %s.%s" % (specs[0][0], specs[0][1][0][0],
                                 specs[1][0], specs[1][1][0][0])
        try:
            bad = fn(db, pred, p12)
        except sqlite3.Error:
            continue
        t += 1
        a += bool(bad)
    return a, t

check("ON-partitioning over a join raises no false alarm",
      join3_alarms(lambda db, p, p12: tlp.join3_check(db, p, p12))[0], 0)
check("the reordering relation raises no false alarm",
      join3_alarms(lambda db, p, p12: tlp.reorder_check(db, p))[0], 0)

# Every table has to appear, or the third one only multiplies the row count
# and this is a two-table join with a spectator.
rnd = random.Random(4)
db = sqlite3.connect(":memory:")
allthree = 0
for _ in range(200):
    specs = tlp.build3(db, rnd, 6, 0.25)
    pred = tlp.gen_join3_predicate(rnd, specs, 2)
    allthree += all(("%s." % n) in pred for n, _, _ in specs)
check("every generated predicate mentions all three tables", allthree, 200)

# How much the generator's two choices -- biasing cross-comparisons toward
# "=" and indexing the first column -- actually affect how often the
# planner reorders.
#
# The first version of this asserted that turning off the equality bias
# measurably lowered the rate, and it PASSED: 58% against 64% at 200 cases.
# At 500 it is 61% against 60%. The assertion was reading noise, and it was
# reading noise about a claim imported from a minimal probe -- two cross
# comparisons and nothing else -- where the two levers really were worth
# 15% against 67%. On the shipped generator they are worth single digits,
# because the generated predicates already carry several cross-table
# comparisons and the planner has reasons to reorder without the help.
#
# So what is asserted now is the COMBINED effect, whose margin is 15 to 19
# points over four seeds, and the individual rates are printed without an
# assertion that the sample cannot support.

def reorder_rate(n=200, seed=0, eq=None, index=True):
    rnd = random.Random(seed)
    db = sqlite3.connect(":memory:")
    saved = tlp.P_JOIN_EQ
    if eq is not None:
        tlp.P_JOIN_EQ = eq
    try:
        moved = t = 0
        for _ in range(n):
            specs = tlp.build3(db, rnd, 6, 0.25, index=index)
            pred = tlp.gen_join3_predicate(rnd, specs, 2)
            try:
                moved += tlp.plans_differ(db, pred)
            except sqlite3.Error:
                continue
            t += 1
        return moved * 100 // t
    finally:
        tlp.P_JOIN_EQ = saved

shipped = reorder_rate(n=400)
no_eq = reorder_rate(n=400, eq=0.0)
no_idx = reorder_rate(n=400, index=False)
neither = reorder_rate(n=400, eq=0.0, index=False)
ok("as shipped the planner reorders %d%% of the time" % shipped) \
    if shipped >= 40 else no("the shipped generator barely reorders",
                             "%d%%" % shipped)
ok("...dropping both the equality bias and the index takes it to %d%%"
   % neither) if neither <= shipped - 8 else \
    no("neither generator choice affects how often the planner reorders",
       "%d%% vs %d%% shipped" % (neither, shipped))
ok("...the index alone is worth %d points (%d%% without it)"
   % (shipped - no_idx, no_idx)) if no_idx < shipped else \
    no("the index does not affect how often the planner reorders",
       "%d%% vs %d%%" % (no_idx, shipped))
# Recorded, NOT asserted: 1 to 6 points across the seeds tried is inside
# what 400 cases can tell apart, and an assertion here would be a test that
# fails on a seed change rather than on a code change.
ok("...the equality bias is worth %d points (%d%% without it), too small "
   "at this sample size to assert" % (shipped - no_eq, no_eq))


def join_detection(kind, n=120, seed=0):
    """Which of the four join relations notices a planted bug."""
    rnd = random.Random(seed)
    real = sqlite3.connect(":memory:")
    hits = dict.fromkeys(("inner", "left", "join3", "reorder"), 0)
    for _ in range(n):
        specs = tlp.build3(real, rnd, 6, 0.25)
        t1, t2, _ = specs
        pred2 = tlp.gen_join_predicate(rnd, t1, t2, 2)
        pred3 = tlp.gen_join3_predicate(rnd, specs, 2)
        p12 = "%s.%s = %s.%s" % (t1[0], t1[1][0][0], t2[0], t2[1][0][0])
        bent = tlp.Corrupt(real, kind)
        probes = (("inner", lambda: tlp.join_check(bent, pred2)),
                  ("left", lambda: tlp.leftjoin_check(bent, pred2)),
                  ("join3", lambda: tlp.join3_check(bent, pred3, p12)),
                  ("reorder", lambda: tlp.reorder_check(bent, pred3)))
        for name, fn in probes:
            try:
                hits[name] += bool(fn())
            except sqlite3.Error:
                pass
    return {k: v * 100 // n for k, v in hits.items()}


# One join order returning different rows from another. Nothing else here
# writes a CROSS JOIN, so nothing else can see it.
r = join_detection("reorder-skew")
ok("a disagreement between join orders is caught by the reordering relation "
   "(%d%%)" % r["reorder"]) if r["reorder"] > 15 else \
    no("a disagreement between join orders is not caught", repr(r))
check("...and no other join relation pins an order, so none can see it",
      [r[k] for k in ("inner", "left", "join3")], [0, 0, 0])

# And the ON-partitioning still works when its left side is itself a join,
# which is the shape where the planner has a choice to make.
d = join_detection("join-drop-pair")
ok("a lost pair is caught over a three-table join too (%d%%)" % d["join3"]) \
    if d["join3"] > 20 else \
    no("the three-table ON-partitioning cannot see a lost pair", repr(d))
check("...and the reordering relation never partitions, so it cannot",
      d["reorder"], 0)

# ------------------------------------------------- LIKE against GLOB
#
# The one DIFFERENTIAL oracle here rather than a metamorphic one, and it
# still needs no second database: SQLite ships two independent pattern
# matchers and they can be pointed at each other.
#
# It rests on two preconditions, and both are the kind that would hold
# quietly for a while and then not. They are checked directly.

# One: LIKE folds ASCII case by default and GLOB never does, so without the
# pragma every comparison between them disagrees on the first capital
# letter. connect() pins it -- and the proof that pinning does something is
# that a connection WITHOUT it answers differently.
pinned = tlp.connect()
bare = sqlite3.connect(":memory:")
check("connect() makes LIKE case-sensitive",
      pinned.execute("SELECT 'A' LIKE 'a'").fetchone()[0], 0)
check("...and a connection without the pragma does not, so it is doing work",
      bare.execute("SELECT 'A' LIKE 'a'").fetchone()[0], 1)
check("GLOB was always case-sensitive",
      pinned.execute("SELECT 'A' GLOB 'a'").fetchone()[0], 0)

# Two: the rewrite % -> * and _ -> ? is sound only over PAT_CHARS. A
# pattern already carrying a GLOB metacharacter means one side reads it as
# a wildcard and the other as a literal.
check("the pattern alphabet contains no GLOB metacharacter",
      [c for c in tlp.PAT_CHARS if c in "*?[]\\"], [])
refused = 0
for pat in ("a*b", "a?b", "a[b]c", "a\\_b"):
    try:
        tlp.like_to_glob(pat)
    except AssertionError:
        refused += 1
check("like_to_glob refuses an untranslatable pattern", refused, 4)

# And the restriction is load-bearing: translated naively, those same
# patterns really do make the two matchers disagree.
disagree = 0
for v, pat in (("axb", "a*b"), ("axb", "a?b"), ("abc", "a[b]c")):
    g = pat.replace("%", "*").replace("_", "?")
    a, b = pinned.execute("SELECT ? LIKE ?, ? GLOB ?", (v, pat, v, g)).fetchone()
    disagree += (a != b)
check("...and translating one anyway would change what is matched",
      disagree, 3)

# With the preconditions met, the two languages agree everywhere -- over
# every pattern the generator can emit, against values that include NULL,
# the empty string, and strings containing the other dialect's wildcards.
vals = [None, "", "a", "A", "aa", "ab", "abc", "a_b", "a%b", "ab*", "x?y", "AB"]
pats = []
rnd = random.Random(0)
for _ in range(60):
    pats.append(tlp.gen_pattern(rnd))
pats += ["a", "a%", "%a", "%a%", "a_", "_a", "a_c", "%", "_", "ab%c", "a%b%"]
mismatch = []
for v in vals:
    for pat in pats:
        a, b = pinned.execute("SELECT ? LIKE ?, ? GLOB ?",
                              (v, pat, v, tlp.like_to_glob(pat))).fetchone()
        if a != b:
            mismatch.append((v, pat, a, b))
check("LIKE and GLOB agree over %d value/pattern pairs, NULL included"
      % (len(vals) * len(pats)), mismatch, [])

# Over generated tables, against a correct engine.
rnd = random.Random(1)
db = tlp.connect()
alarms = tested = split = 0
for _ in range(250):
    _, cols, rows = tlp.build(db, rnd, 8, 0.25)
    text = [c for c, t in cols if t == "TEXT"]
    if not text:
        continue
    col = rnd.choice(text)
    pat = tlp.gen_pattern(rnd)
    try:
        bad = tlp.like_glob_check(db, col, pat)
        hit = db.execute("SELECT COUNT(*) FROM t WHERE %s LIKE '%s'"
                         % (col, pat)).fetchone()[0]
    except sqlite3.Error:
        continue
    tested += 1
    alarms += bool(bad)
    split += (0 < hit < len(rows))
check("the LIKE/GLOB relation raises no false alarm", alarms, 0)
pct = split * 100 // tested
# A pattern matching no row, or every row, makes both matchers agree
# trivially. That is this relation's vacuity question -- not
# three-valuedness, which it never asks about.
ok("patterns split the table %d%% of the time, so the agreement is not "
   "trivial" % pct) if pct >= 20 else \
    no("patterns almost never split the table", "%d%%" % pct)

# And pattern matching now reaches every other oracle too, because the
# shared atom generator emits it -- which is the point of adding it rather
# than bolting on one more relation.
rnd = random.Random(7)
db = tlp.connect()
_, cols, _ = tlp.build(db, rnd, 8, 0.25)
withpat = 0
for _ in range(400):
    withpat += ("LIKE" in tlp.gen_predicate(rnd, cols, 2)
                or "GLOB" in tlp.gen_predicate(rnd, cols, 2))
ok("pattern matching reaches the shared predicate generator (%d of 400)"
   % withpat) if withpat > 40 else \
    no("the shared generator almost never emits a pattern match",
       "%d of 400" % withpat)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
