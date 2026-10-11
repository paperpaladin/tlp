#!/usr/bin/env python3
"""tlp -- find queries where SQLite returns the wrong rows.

Not crashes. Wrong answers: a query whose result differs from what the data
says it should be, with no error and nothing to compare against. That class
is invisible to ordinary testing, because there is no second implementation
to disagree with and no exception to catch.

The way out is a *metamorphic* oracle -- a relation the engine must satisfy
with ITSELF, so no reference implementation is needed at all.

TLP, ternary logic partitioning (Rigger and Su, OOPSLA 2020). SQL is
three-valued, so for any predicate p every row of t makes p exactly one of
TRUE, FALSE or NULL. Therefore:

    SELECT * FROM t WHERE p
      UNION ALL SELECT * FROM t WHERE NOT p
      UNION ALL SELECT * FROM t WHERE p IS NULL

must be the same multiset as SELECT * FROM t. Always. Any predicate, any
data, any engine. If it is not, the optimiser dropped or duplicated a row.

The three partitions are also why the predicate generator matters more than
it looks: a predicate that is never NULL leaves the third arm empty, and the
test degenerates into "the engine agrees with itself about TRUE and FALSE",
which it will. Measuring that is what `tlp vacuity` is for.

    tlp.py truth [--depth N]   three-valued logic, exhaustively
"""
import argparse
import itertools
import sqlite3
import sys

# --------------------------------------------------------------- 3VL model
#
# The SQL standard's truth tables, transcribed. This is the one place in the
# project where the expected answer is written down by hand rather than
# taken from an authority, so it is worth being precise about what that
# buys: it is a CALIBRATION, not the oracle. The fuzzer's oracle is the
# metamorphic relation below, which needs no model at all. This exists to
# prove the model of three-valued logic the generator is built on matches
# the engine, before anything is built on it.
#
# Python's True/False/None stand for SQL's TRUE/FALSE/NULL.

TRI = (True, False, None)


def t_not(a):
    return None if a is None else not a


def t_and(a, b):
    # FALSE dominates: FALSE AND NULL is FALSE, not NULL, because no value
    # of the unknown operand could make the conjunction true.
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def t_or(a, b):
    # TRUE dominates, by the mirror argument.
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def t_is(a, b):
    # IS compares the three values themselves, so it is total: it never
    # yields NULL. This is the operator that lets a query ask about NULL
    # rather than propagate it.
    return a is b


def t_eq(a, b):
    # = propagates. NULL = NULL is NULL, which is the single fact most
    # often got wrong by people and by optimisers alike.
    if a is None or b is None:
        return None
    return a == b


# Unary postfix tests, all total.
UNARY_TESTS = {
    "IS NULL": lambda a: a is None,
    "IS NOT NULL": lambda a: a is not None,
    "IS TRUE": lambda a: a is True,
    "IS FALSE": lambda a: a is False,
    "IS NOT TRUE": lambda a: a is not True,
    "IS NOT FALSE": lambda a: a is not False,
}

BINARY_OPS = {
    "AND": t_and,
    "OR": t_or,
    "IS": t_is,
    "IS NOT": lambda a, b: not t_is(a, b),
    "=": t_eq,
    "<>": lambda a, b: None if t_eq(a, b) is None else not t_eq(a, b),
}

# How each three-valued constant is written in SQL. SQLite accepts the TRUE
# and FALSE keywords since 3.23; using them rather than 1 and 0 keeps the
# generated text readable and exercises the keyword path.
LITERAL = {True: "TRUE", False: "FALSE", None: "NULL"}


def enumerate_exprs(depth):
    """Every expression of the grammar up to `depth`, as (sql, value) pairs.

    Exhaustive rather than random, because the space is small enough to
    cover completely and a sampled truth table is not a truth table.
    """
    level = [(LITERAL[v], v) for v in TRI]
    seen = list(level)
    for _ in range(depth):
        nxt = []
        for sql, val in level:
            nxt.append(("(NOT %s)" % sql, t_not(val)))
            for name, fn in UNARY_TESTS.items():
                nxt.append(("(%s %s)" % (sql, name), fn(val)))
        for (asql, aval), (bsql, bval) in itertools.product(level, repeat=2):
            for name, fn in BINARY_OPS.items():
                nxt.append(("(%s %s %s)" % (asql, name, bsql), fn(aval, bval)))
        seen.extend(nxt)
        level = nxt
    return seen


def sql_value(db, sql):
    """What SQLite says the expression is, as a three-valued Python value."""
    (raw,) = db.execute("SELECT " + sql).fetchone()
    if raw is None:
        return None
    return bool(raw)


# ------------------------------------------------------- schema and data
#
# Two decisions here do all the work, and both are about making comparisons
# COLLIDE.
#
# The value domain is tiny -- integers 0..4, three short strings. Random
# 64-bit integers would make "a = b" false for every pair of rows that ever
# existed, and a predicate that is false everywhere partitions nothing. A
# small domain is what makes equality, ordering and IN interesting.
#
# And NULLs are frequent by construction, not by accident. A table without
# them cannot exercise three-valued logic at all, so the rate is a
# parameter. At 0 the whole technique degenerates and `tlp vacuity` says so.

INTS = [0, 1, 2, 3, 4]
TEXTS = ["a", "bb", "c"]


def gen_schema(rnd):
    """A table of two to four columns, mixed INTEGER and TEXT affinity."""
    ncols = rnd.randint(2, 4)
    cols = []
    for i in range(ncols):
        cols.append(("c%d" % i, rnd.choice(["INTEGER", "TEXT"])))
    ddl = "CREATE TABLE t (%s)" % ", ".join("%s %s" % c for c in cols)
    return ddl, cols


def gen_rows(rnd, cols, nrows, null_rate):
    rows = []
    for _ in range(nrows):
        row = []
        for _, ty in cols:
            if rnd.random() < null_rate:
                row.append(None)
            elif ty == "INTEGER":
                row.append(rnd.choice(INTS))
            else:
                row.append(rnd.choice(TEXTS))
        rows.append(tuple(row))
    return rows


def build(db, rnd, nrows=8, null_rate=0.25):
    ddl, cols = gen_schema(rnd)
    db.execute("DROP TABLE IF EXISTS t")
    db.execute(ddl)
    rows = gen_rows(rnd, cols, nrows, null_rate)
    db.executemany("INSERT INTO t VALUES (%s)" % ",".join("?" * len(cols)), rows)
    return ddl, cols, rows


# ---------------------------------------------------------- the predicate
#
# What this emits decides whether any of the rest means anything, which is
# why `tlp vacuity` exists and why it is built before the oracles are.

CMP = ["=", "<>", "<", "<=", ">", ">="]


def gen_atom(rnd, cols):
    name, ty = rnd.choice(cols)
    k = rnd.random()
    if k < 0.12:
        return "%s IS NULL" % name
    if k < 0.20:
        return "%s IS NOT NULL" % name
    if k < 0.32:
        # IN with a NULL inside is the classic three-valued trap: a
        # non-match against a list containing NULL is NULL, not FALSE.
        vals = [lit(rnd, ty) for _ in range(rnd.randint(1, 3))]
        if rnd.random() < 0.4:
            vals.append("NULL")
        return "%s IN (%s)" % (name, ", ".join(vals))
    if k < 0.42:
        lo, hi = sorted([lit(rnd, ty), lit(rnd, ty)])
        return "%s BETWEEN %s AND %s" % (name, lo, hi)
    if k < 0.55 and len(cols) > 1:
        other, oty = rnd.choice(cols)
        return "%s %s %s" % (name, rnd.choice(CMP), other)
    return "%s %s %s" % (name, rnd.choice(CMP), lit(rnd, ty))


def lit(rnd, ty):
    if rnd.random() < 0.10:
        return "NULL"
    if ty == "INTEGER":
        return str(rnd.choice(INTS))
    return "'%s'" % rnd.choice(TEXTS)


# A predicate is generated as a TREE and rendered on demand, because the
# reducer has to be able to replace a node with one of its own subtrees and
# a finished string cannot be taken apart again without parsing it back.
# Everything else still works in strings; gen_predicate() is the same
# function it always was, with one render() in front of it.
#
#   ("atom", text) | ("not", a) | ("and", a, b) | ("or", a, b)


def render(node):
    k = node[0]
    if k == "atom":
        return node[1]
    if k == "not":
        return "(NOT %s)" % render(node[1])
    return "(%s %s %s)" % (render(node[1]), k.upper(), render(node[2]))


def tree_size(node):
    return 1 + sum(tree_size(c) for c in node[1:] if isinstance(c, tuple))


def subtrees(node):
    """Every proper subtree. Candidates for replacing the whole."""
    out = []
    for child in node[1:]:
        if isinstance(child, tuple):
            out.append(child)
            out.extend(subtrees(child))
    return out


def gen_tree(rnd, cols, depth=2, atom=None):
    """A predicate tree. `atom` defaults to the row-level atom generator.

    Parameterised so HAVING predicates -- which are built from aggregates
    rather than from columns -- get the same AND/OR/NOT structure, and
    therefore the same reducer, for free.
    """
    atom = atom or gen_atom
    if depth <= 0 or rnd.random() < 0.3:
        return ("atom", atom(rnd, cols))
    k = rnd.random()
    if k < 0.20:
        return ("not", gen_tree(rnd, cols, depth - 1, atom))
    return ("and" if k < 0.60 else "or",
            gen_tree(rnd, cols, depth - 1, atom),
            gen_tree(rnd, cols, depth - 1, atom))


def gen_predicate(rnd, cols, depth=2):
    return render(gen_tree(rnd, cols, depth))


# ------------------------------------------------------------- vacuity
#
# The measurement that decides whether the oracles are testing anything.
#
# TLP splits the rows three ways. If a predicate is never NULL the third
# arm is always empty and the relation collapses to "the engine agrees with
# itself about TRUE and FALSE", which it will, forever, while the run
# prints a large and encouraging number. If a predicate is constant across
# every row then two arms are empty and nothing is being partitioned at
# all.
#
# So: how often is a generated predicate genuinely three-valued?


def partition_sizes(db, pred):
    n = lambda w: db.execute("SELECT COUNT(*) FROM t WHERE " + w).fetchone()[0]
    return n("(%s)" % pred), n("NOT (%s)" % pred), n("(%s) IS NULL" % pred)


def cmd_vacuity(args):
    import random
    rnd = random.Random(args.seed)
    db = sqlite3.connect(":memory:")

    three = 0      # all three partitions non-empty
    has_null = 0   # the NULL arm is non-empty
    constant = 0   # one arm holds every row
    total = 0

    for _ in range(args.n):
        _, cols, rows = build(db, rnd, args.rows, args.null_rate)
        pred = gen_predicate(rnd, cols, args.depth)
        try:
            t, f, u = partition_sizes(db, pred)
        except sqlite3.Error:
            continue
        total += 1
        if u:
            has_null += 1
        if t and f and u:
            three += 1
        if max(t, f, u) == len(rows):
            constant += 1

    pct = lambda k: (100.0 * k / total) if total else 0.0
    print("%d predicates over %d-row tables, NULL rate %.2f" %
          (total, args.rows, args.null_rate))
    print("  %5.1f%%  have a non-empty NULL partition" % pct(has_null))
    print("  %5.1f%%  are genuinely three-valued (all three non-empty)" % pct(three))
    print("  %5.1f%%  are constant across every row (partition nothing)" % pct(constant))
    return 0


# --------------------------------------------------------------- oracles
#
# Neither of these compares against a reference. Each is a relation the
# engine must satisfy with itself, so a disagreement is a bug in SQLite --
# or, far more likely here, a bug in this file.


def rows_of(db, sql):
    """A result as a sorted multiset.

    Sorted, because SQL does not promise an order without ORDER BY and a
    comparison between sequences would report a bug every time the planner
    legitimately changed its mind. A multiset rather than a set, because
    UNION ALL keeps duplicates and dropping them would hide exactly the
    kind of bug this is looking for.
    """
    return sorted(db.execute(sql).fetchall(), key=repr)


def tlp_check(db, pred):
    """The whole must equal the three partitions put back together."""
    whole = rows_of(db, "SELECT * FROM t")
    parts = rows_of(db,
                    "SELECT * FROM t WHERE (%s) "
                    "UNION ALL SELECT * FROM t WHERE NOT (%s) "
                    "UNION ALL SELECT * FROM t WHERE (%s) IS NULL"
                    % (pred, pred, pred))
    if whole == parts:
        return None
    return ("TLP", len(whole), len(parts))


def norec_check(db, pred):
    """Counting rows with the optimiser on and with it unable to help.

    The predicate in a WHERE clause can be answered from an index. The same
    predicate in the SELECT list of a subquery cannot -- every row has to
    be fetched and evaluated. The two counts must agree, and when they do
    not it is the index path that is wrong, which is the half no amount of
    staring at the query will show you.
    """
    (opt,) = db.execute("SELECT COUNT(*) FROM t WHERE (%s)" % pred).fetchone()
    (unopt,) = db.execute(
        "SELECT SUM(w) FROM (SELECT ((%s) IS TRUE) AS w FROM t)" % pred
    ).fetchone()
    unopt = unopt or 0
    if opt == unopt:
        return None
    return ("NoREC", opt, unopt)


def run_oracle(args, check, label):
    """Shared driver. Reports, and says what it did when it finds nothing."""
    import random
    rnd = random.Random(args.seed)
    db = sqlite3.connect(":memory:")

    found = 0
    tested = 0
    skipped = 0
    interesting = 0

    for i in range(args.n):
        ddl, cols, rows = build(db, rnd, args.rows, args.null_rate)
        if args.index and len(cols) > 0:
            # NoREC needs something for the optimiser to reach for.
            db.execute("CREATE INDEX IF NOT EXISTS i0 ON t(%s)" % cols[0][0])
        pred = gen_predicate(rnd, cols, args.depth)
        try:
            t, f, u = partition_sizes(db, pred)
            bad = check(db, pred)
        except sqlite3.Error:
            skipped += 1
            continue
        tested += 1
        if t and f and u:
            interesting += 1
        if bad:
            found += 1
            print("FOUND  %s disagreement on seed step %d" % (bad[0], i))
            print("  %s" % ddl)
            for r in rows:
                print("  INSERT INTO t VALUES (%s);"
                      % ", ".join(sql_literal(v) for v in r))
            print("  predicate: %s" % pred)
            print("  %s says %r, the other form says %r" % (bad[0], bad[1], bad[2]))
            if found >= args.max_reports:
                break

    # A run that finds nothing has to be distinguishable from a run that
    # tested nothing, so the shape of what was tried is printed either way.
    print("%s: %d queries, %d disagreements, %d skipped, "
          "%d genuinely three-valued (%.0f%%)"
          % (label, tested, found, skipped, interesting,
             100.0 * interesting / tested if tested else 0.0))
    return 1 if found else 0


def cmd_tlp(args):
    return run_oracle(args, tlp_check, "TLP")


def cmd_norec(args):
    return run_oracle(args, norec_check, "NoREC")


# -------------------------------------------------------------- reducing
#
# A found case is a schema, eight rows and a predicate with seven nodes in
# it. Nobody reads that, which makes it the same as no report. Shrinking is
# not a nicety here; it is what turns the output into something a person
# can act on.
#
# Greedy delta debugging over three axes, repeated until none of them
# moves:
#
#   rows        drop one, keep the drop if the case still fails
#   predicate   replace a node with one of its own subtrees
#   values      pull a literal toward NULL, 0 or the empty string
#
# The one invariant that matters is SOUNDNESS: whatever comes out must
# still fail. A reducer that shrinks past the bug produces a minimal case
# that reproduces nothing, and sends whoever reads it looking for a bug
# that is not there -- worse than printing the unreduced version. The suite
# asserts it on every reduction it performs.


def sql_literal(v):
    """A Python value as the SQL that produces it.

    The point of a reduced case is that someone can paste it into sqlite3,
    and Python's repr of a row gives `(None, 'aa')` -- which is not SQL.
    """
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'%s'" % v.replace("'", "''")
    return str(v)


def simpler_than(v, ty):
    """The candidate values strictly below `v` in the simplicity order.

    NULL, then 0 or the empty string, then everything else. STRICTLY below:
    offering every candidate for every value instead makes the shrink
    oscillate -- NULL becomes 0, then 0 becomes NULL, each one counting as
    progress -- and the reducer never returns. That is not hypothetical,
    it is what the first version of this did, and it is why a shrink needs
    a well-founded order rather than a set of rewrites that each look like
    an improvement.
    """
    floor = 0 if ty == "INTEGER" else ""
    if v is None:
        return ()
    if v == floor and type(v) is type(floor):
        return (None,)
    return (None, floor)


def reduce_case(fails, cols, rows, tree):
    """Shrink (rows, tree) while `fails(rows, predicate_text)` holds.

    `fails` rebuilds the table and re-runs the oracle, so this makes no
    assumption about which oracle found the case or what the bug is.
    """
    if not fails(rows, render(tree)):
        raise ValueError("the case does not fail, so there is nothing to reduce")

    moved = True
    while moved:
        moved = False

        # Rows first: they dominate how a report reads.
        i = 0
        while i < len(rows):
            trial = rows[:i] + rows[i + 1:]
            if trial and fails(trial, render(tree)):
                rows = trial
                moved = True
            else:
                i += 1

        # Then the predicate, trying the largest subtree first so that one
        # accepted replacement removes as much as possible.
        for sub in sorted(subtrees(tree), key=tree_size, reverse=True):
            if fails(rows, render(sub)):
                tree = sub
                moved = True
                break

        # Then the values. A value that can be simplified was never part of
        # what made the case fail, and "0" reads better than "3" in a
        # report that someone has to reason about.
        for i in range(len(rows)):
            for j, (_, ty) in enumerate(cols):
                for cand in simpler_than(rows[i][j], ty):
                    trial = list(rows)
                    trial[i] = rows[i][:j] + (cand,) + rows[i][j + 1:]
                    if fails(trial, render(tree)):
                        rows = trial
                        moved = True
                        break

    return rows, tree


def cmd_reduce(args):
    """Find a case against a planted bug, then shrink it.

    Pointed at a planted bug rather than at SQLite because there are no
    real cases to reduce -- and because a reducer can only be tested
    against a failure whose cause is already known.
    """
    import random
    rnd = random.Random(args.seed)
    db = sqlite3.connect(":memory:")

    for i in range(args.tries):
        _, cols, rows = build(db, rnd, args.rows, args.null_rate)
        tree = gen_tree(rnd, cols, args.depth)

        def fails(trial_rows, pred, cols=cols):
            db.execute("DROP TABLE IF EXISTS t")
            db.execute("CREATE TABLE t (%s)" % ", ".join("%s %s" % c for c in cols))
            db.executemany("INSERT INTO t VALUES (%s)" % ",".join("?" * len(cols)),
                           trial_rows)
            try:
                return tlp_check(Corrupt(db, args.bug), pred) is not None
            except sqlite3.Error:
                return False

        if not fails(rows, render(tree)):
            continue

        before = (len(rows), tree_size(tree))
        rows2, tree2 = reduce_case(fails, cols, list(rows), tree)

        print("found at try %d, against the planted bug %r" % (i + 1, args.bug))
        print("  rows       %d -> %d" % (before[0], len(rows2)))
        print("  predicate  %d nodes -> %d" % (before[1], tree_size(tree2)))
        print()
        print("CREATE TABLE t (%s);" % ", ".join("%s %s" % c for c in cols))
        for r in rows2:
            print("INSERT INTO t VALUES (%s);" % ", ".join(sql_literal(v) for v in r))
        print("-- predicate: %s" % render(tree2))
        print("-- still fails after reduction: %s" % fails(rows2, render(tree2)))
        return 1

    print("no case found in %d tries" % args.tries)
    return 0


# ------------------------------------------------------ join partitioning
#
# The published work found more bugs partitioning a join's ON clause than
# anywhere else, and the reason is worth stating: a WHERE predicate is
# evaluated once per row, but an ON predicate decides which PAIRS of rows
# exist at all, so getting it wrong changes the shape of the result rather
# than its contents. Join reordering, index selection on the inner side and
# the outer-join null-padding rules all meet in one clause.
#
# Two relations, both derived and checked by hand before anything was built
# on them rather than taken from the paper on faith.
#
# INNER. Every pair in the cross product makes the ON predicate exactly one
# of TRUE, FALSE or NULL, and an inner join keeps precisely the TRUE pairs.
# So the three ON-partitions are a partition of the cross product:
#
#     t1 JOIN t2 ON p
#       UNION ALL t1 JOIN t2 ON NOT p
#       UNION ALL t1 JOIN t2 ON p IS NULL
#     ==  t1, t2
#
# LEFT. The same partitioning does NOT work, because each arm pads its own
# unmatched rows and the padding is counted three times. What is true is a
# decomposition: a left join is the inner join plus one row for every left
# row that found no TRUE match anywhere.
#
#     COUNT(t1 LEFT JOIN t2 ON p)
#     ==  COUNT(t1 JOIN t2 ON p)
#       + COUNT(t1 WHERE NOT EXISTS (SELECT 1 FROM t2 WHERE p))
#
# That is a different shape of query from anything above, which is the
# point: NOT EXISTS is a correlated subquery and the optimiser rewrites it
# into something else entirely.


def build2(db, rnd, nrows=6, null_rate=0.25):
    """Two tables whose columns share a value domain, so that a comparison
    across them is sometimes true. Disjoint domains would make every
    cross-table predicate FALSE and the join trivially empty."""
    tables = []
    for name, prefix in (("t1", "a"), ("t2", "b")):
        ncols = rnd.randint(2, 3)
        cols = [("%s%d" % (prefix, i), rnd.choice(["INTEGER", "TEXT"]))
                for i in range(ncols)]
        db.execute("DROP TABLE IF EXISTS %s" % name)
        db.execute("CREATE TABLE %s (%s)" % (name, ", ".join("%s %s" % c for c in cols)))
        rows = gen_rows(rnd, cols, nrows, null_rate)
        db.executemany("INSERT INTO %s VALUES (%s)" % (name, ",".join("?" * len(cols))),
                       rows)
        tables.append((name, cols, rows))
    return tables


def gen_join_predicate(rnd, t1, t2, depth=2):
    """A predicate over both tables.

    At least one cross-table comparison is forced. Without that the
    predicate is a filter on one side that the planner can push down, the
    join degenerates, and the partitioning tests a much less interesting
    query than it appears to.
    """
    n1, c1, _ = t1
    n2, c2, _ = t2

    def cross():
        a, aty = rnd.choice(c1)
        b, bty = rnd.choice(c2)
        return "%s.%s %s %s.%s" % (n1, a, rnd.choice(CMP), n2, b)

    def side():
        name, cols = (n1, c1) if rnd.random() < 0.5 else (n2, c2)
        col, ty = rnd.choice(cols)
        if rnd.random() < 0.25:
            return "%s.%s IS NULL" % (name, col)
        return "%s.%s %s %s" % (name, col, rnd.choice(CMP), lit(rnd, ty))

    def expr(d):
        if d <= 0 or rnd.random() < 0.35:
            return cross() if rnd.random() < 0.6 else side()
        k = rnd.random()
        if k < 0.18:
            return "(NOT %s)" % expr(d - 1)
        op = "AND" if k < 0.60 else "OR"
        return "(%s %s %s)" % (expr(d - 1), op, expr(d - 1))

    pred = expr(depth)
    if ("%s." % n1) not in pred or ("%s." % n2) not in pred:
        pred = "(%s AND %s)" % (pred, cross())
    return pred


def join_check(db, pred):
    """The three ON-partitions must reassemble into the cross product."""
    whole = rows_of(db, "SELECT * FROM t1, t2")
    parts = rows_of(db,
                    "SELECT * FROM t1 JOIN t2 ON (%s) "
                    "UNION ALL SELECT * FROM t1 JOIN t2 ON NOT (%s) "
                    "UNION ALL SELECT * FROM t1 JOIN t2 ON (%s) IS NULL"
                    % (pred, pred, pred))
    if whole == parts:
        return None
    return ("JOIN", len(whole), len(parts))


def leftjoin_check(db, pred):
    """A left join is its inner join plus the left rows that matched nothing."""
    (left,) = db.execute(
        "SELECT COUNT(*) FROM t1 LEFT JOIN t2 ON (%s)" % pred).fetchone()
    (inner,) = db.execute(
        "SELECT COUNT(*) FROM t1 JOIN t2 ON (%s)" % pred).fetchone()
    (unmatched,) = db.execute(
        "SELECT COUNT(*) FROM t1 WHERE NOT EXISTS "
        "(SELECT 1 FROM t2 WHERE (%s))" % pred).fetchone()
    if left == inner + unmatched:
        return None
    return ("LEFT JOIN", left, inner + unmatched)


def cmd_join(args):
    import random
    rnd = random.Random(args.seed)
    db = sqlite3.connect(":memory:")
    found = tested = skipped = interesting = 0

    for i in range(args.n):
        t1, t2 = build2(db, rnd, args.rows, args.null_rate)
        if args.index:
            db.execute("CREATE INDEX IF NOT EXISTS i1 ON t1(%s)" % t1[1][0][0])
            db.execute("CREATE INDEX IF NOT EXISTS i2 ON t2(%s)" % t2[1][0][0])
        pred = gen_join_predicate(rnd, t1, t2, args.depth)
        try:
            # A join predicate is interesting when it actually splits the
            # cross product rather than selecting all of it or none.
            (kept,) = db.execute(
                "SELECT COUNT(*) FROM t1 JOIN t2 ON (%s)" % pred).fetchone()
            (total,) = db.execute("SELECT COUNT(*) FROM t1, t2").fetchone()
            bad = join_check(db, pred) or leftjoin_check(db, pred)
        except sqlite3.Error:
            skipped += 1
            continue
        tested += 1
        if 0 < kept < total:
            interesting += 1
        if bad:
            found += 1
            print("FOUND  %s disagreement at step %d" % (bad[0], i))
            for name, cols, rows in (t1, t2):
                print("  CREATE TABLE %s (%s);" %
                      (name, ", ".join("%s %s" % c for c in cols)))
                for r in rows:
                    print("    INSERT INTO %s VALUES %r;" % (name, (r,)))
            print("  ON %s" % pred)
            print("  %r against %r" % (bad[1], bad[2]))
            if found >= args.max_reports:
                break

    print("join: %d queries, %d disagreements, %d skipped, "
          "%d split the cross product (%.0f%%)"
          % (tested, found, skipped, interesting,
             100.0 * interesting / tested if tested else 0.0))
    return 1 if found else 0


# ------------------------------------------------- aggregate partitioning
#
# Row-level TLP asks whether the partitions hold the same ROWS. This asks
# whether an aggregate computed over the whole table equals the same
# aggregate recombined from the three partitions -- which is a different
# question, and catches bugs the row form cannot see, because an aggregate
# can be wrong while every row is present.
#
# The interesting part is that there is no single recombination rule. Each
# aggregate composes differently, and getting one wrong produces a tool
# that reports bugs in a correct database:
#
#   SUM      add the partition sums.    SUM of nothing is NULL, not 0, and
#                                       the outer SUM skips NULLs, so an
#                                       empty partition contributes nothing
#                                       and the arithmetic still works.
#   COUNT    ADD the partition counts, not count them. COUNT of nothing is
#            0, so this one needs no care, which is exactly why writing it
#            by analogy with SUM is a mistake waiting to happen.
#   MIN      take the MIN of the partition MINs. MIN ignores NULLs at both
#            levels, which is what makes the two levels agree.
#   MAX      the mirror of MIN.
#   AVG      NOT decomposable this way. The average of three averages is
#            not the average, and weighting them needs the counts -- so it
#            is left out rather than approximated.

AGGREGATES = {
    "SUM": "SUM",     # outer combiner for the partition results
    "COUNT": "SUM",   # counts ADD, they do not count
    "MIN": "MIN",
    "MAX": "MAX",
}


def agg_check(db, pred, agg, col):
    """An aggregate over the whole table against the same one recombined."""
    outer = AGGREGATES[agg]
    (whole,) = db.execute("SELECT %s(%s) FROM t" % (agg, col)).fetchone()
    (parts,) = db.execute(
        "SELECT %s(a) FROM ("
        "  SELECT %s(%s) AS a FROM t WHERE (%s)"
        "  UNION ALL SELECT %s(%s) FROM t WHERE NOT (%s)"
        "  UNION ALL SELECT %s(%s) FROM t WHERE (%s) IS NULL)"
        % (outer, agg, col, pred, agg, col, pred, agg, col, pred)
    ).fetchone()
    if whole == parts:
        return None
    return ("agg-%s" % agg, whole, parts)


def cmd_agg(args):
    import random
    rnd = random.Random(args.seed)
    db = sqlite3.connect(":memory:")
    found = tested = skipped = 0
    per_agg = {a: 0 for a in AGGREGATES}

    for i in range(args.n):
        ddl, cols, rows = build(db, rnd, args.rows, args.null_rate)
        pred = gen_predicate(rnd, cols, args.depth)
        # SUM and COUNT over TEXT coerce in ways that are SQLite's business
        # rather than the standard's, so the column is chosen to suit the
        # aggregate instead of pretending that question does not exist.
        ints = [c for c, ty in cols if ty == "INTEGER"]
        for agg in AGGREGATES:
            if agg in ("SUM",) and not ints:
                continue
            col = rnd.choice(ints) if agg == "SUM" else rnd.choice(cols)[0]
            try:
                bad = agg_check(db, pred, agg, col)
            except sqlite3.Error:
                skipped += 1
                continue
            tested += 1
            per_agg[agg] += 1
            if bad:
                found += 1
                print("FOUND  %s disagreement at step %d" % (bad[0], i))
                print("  %s" % ddl)
                for r in rows:
                    print("  INSERT INTO t VALUES (%s);"
                          % ", ".join(sql_literal(v) for v in r))
                print("  predicate: %s" % pred)
                print("  whole %r, recombined %r" % (bad[1], bad[2]))
                if found >= args.max_reports:
                    return 1

    print("aggregate TLP: %d checks, %d disagreements, %d skipped" %
          (tested, found, skipped))
    print("  per aggregate: %s" %
          ", ".join("%s %d" % (a, n) for a, n in sorted(per_agg.items())))
    return 1 if found else 0


# --------------------------------------------------------------- HAVING
#
# HAVING is the one that does not partition rows at all, which is why it
# gets its own relation rather than a flag on the GROUP BY one.
#
# A WHERE predicate decides which ROWS survive. An ON predicate decides
# which PAIRS exist. A HAVING predicate decides which GROUPS survive -- and
# the groups are formed by the GROUP BY, before HAVING ever runs. So every
# group makes the HAVING predicate exactly one of TRUE, FALSE or NULL, and
# partitioning it splits the groups three ways while leaving every group
# itself untouched.
#
# That makes the recombination the SIMPLEST one here, not the hardest, and
# in the exact place where item 09 needed the most care:
#
#     SELECT g, COUNT(*) FROM t GROUP BY g HAVING h
#       UNION ALL SELECT g, COUNT(*) FROM t GROUP BY g HAVING NOT h
#       UNION ALL SELECT g, COUNT(*) FROM t GROUP BY g HAVING h IS NULL
#     ==  SELECT g, COUNT(*) FROM t GROUP BY g
#
# Plain UNION ALL, and no re-aggregation. The GROUP BY relation needed
# SUM(n) over the arms because partitioning a WHERE clause cuts every group
# into three pieces that have to be added back together. Partitioning a
# HAVING clause cuts no group at all: each one arrives whole in exactly one
# arm, already counted. Same table, same three-way split, and the right
# rule is different again because what is being split is different.
#
# The subtlety is in the generator instead. A HAVING predicate is built
# from aggregates, and an aggregate is NULL in ways a column is not: SUM,
# MIN and MAX of a group whose values are all NULL are NULL, while
# COUNT(*) never is and COUNT(c) never is. So the third arm has substance
# only if the generated predicates reach for the aggregates that can be
# NULL, which is measured rather than assumed.

# How often a HAVING atom is built from a COUNT rather than from SUM, MIN
# or MAX. It matters because the two kinds differ in exactly the way the
# third partition depends on: SUM, MIN and MAX of a group whose values are
# all NULL are NULL, while COUNT(*) and COUNT(c) never are. A generator
# made only of COUNTs leaves the NULL arm permanently empty; one with no
# COUNTs at all loses every predicate that is reliably two-valued, and the
# TRUE and FALSE arms thin out instead.
#
# 0.10 is measured, not chosen. The sweep is in the README, and it has an
# interior optimum in both parameters for the same reason the row-level
# NULL rate does.
P_COUNT_TERM = 0.10


def gen_having_atom(rnd, cols):
    """One comparison between aggregates, or an aggregate against a literal."""
    ints = [c for c, ty in cols if ty == "INTEGER"]
    if not ints or rnd.random() < P_COUNT_TERM:
        term = "COUNT(*)" if not ints or rnd.random() < 0.6 \
            else "COUNT(%s)" % rnd.choice(ints)
    else:
        c = rnd.choice(ints)
        term = rnd.choice(["SUM(%s)" % c, "MIN(%s)" % c, "MAX(%s)" % c])
    k = rnd.random()
    if k < 0.15:
        return "%s IS NULL" % term
    if k < 0.22:
        return "%s IS NOT NULL" % term
    return "%s %s %s" % (term, rnd.choice(CMP), rnd.choice(INTS))


def gen_having(rnd, cols, depth=2):
    return render(gen_tree(rnd, cols, depth, gen_having_atom))


def having_group_sizes(db, pred, col):
    """How many GROUPS land in each of the three HAVING partitions."""
    n = lambda h: db.execute(
        "SELECT COUNT(*) FROM (SELECT %s FROM t GROUP BY %s HAVING %s)"
        % (col, col, h)).fetchone()[0]
    return (n("(%s)" % pred), n("NOT (%s)" % pred), n("(%s) IS NULL" % pred))


HAVING_ARMS = ("(%s)", "NOT (%s)", "(%s) IS NULL")


def having_check(db, pred, col, combine="UNION ALL", arms=HAVING_ARMS):
    """The grouped whole against the same groups split three ways.

    `combine` and `arms` are parameters so the suite can put the wrong rule
    in and watch the relation go wrong. Their defaults are what ships, and
    the suite checks the defaults separately -- a parameterised test that
    always supplies the parameter stops testing what ships.
    """
    whole = rows_of(db, "SELECT %s, COUNT(*) FROM t GROUP BY %s" % (col, col))
    parts = rows_of(db,
                    (" %s " % combine).join(
                        "SELECT %s, COUNT(*) FROM t GROUP BY %s HAVING %s"
                        % (col, col, arm % pred) for arm in arms))
    if whole == parts:
        return None
    return ("HAVING", len(whole), len(parts))


def cmd_having(args):
    """Its own driver, because the vacuity question is about GROUPS here.

    run_oracle reports how often a predicate splits the ROWS three ways,
    which is the wrong measurement for a clause that never looks at a row.
    """
    import random
    rnd = random.Random(args.seed)
    db = sqlite3.connect(":memory:")
    found = tested = skipped = interesting = 0

    for i in range(args.n):
        ddl, cols, rows = build(db, rnd, args.rows, args.null_rate)
        col = cols[0][0]
        pred = gen_having(rnd, cols, args.depth)
        try:
            t, f, u = having_group_sizes(db, pred, col)
            bad = having_check(db, pred, col)
        except sqlite3.Error:
            skipped += 1
            continue
        tested += 1
        if t and f and u:
            interesting += 1
        if bad:
            found += 1
            print("FOUND  HAVING disagreement on seed step %d" % i)
            print("  %s" % ddl)
            for r in rows:
                print("  INSERT INTO t VALUES (%s);"
                      % ", ".join(sql_literal(v) for v in r))
            print("  GROUP BY %s HAVING %s" % (col, pred))
            print("  whole %r groups, recombined %r" % (bad[1], bad[2]))
            if found >= args.max_reports:
                break

    print("HAVING: %d queries, %d disagreements, %d skipped, "
          "%d split the groups three ways (%.0f%%)"
          % (tested, found, skipped, interesting,
             100.0 * interesting / tested if tested else 0.0))
    return 1 if found else 0


# --------------------------------------------- DISTINCT and GROUP BY
#
# Both are the partitioning done over a query shape where the obvious
# composition rule is the wrong one, which is the same trap as the
# aggregate table and worth meeting twice.
#
# DISTINCT. The three partitions still cover every row, so every distinct
# value of a column appears in at least one arm. But a value can appear in
# TWO arms -- two different rows can share a value and land on opposite
# sides of the predicate -- so the arms are recombined with UNION, which is
# a set union, rather than the UNION ALL every other relation here uses:
#
#     SELECT DISTINCT c FROM t WHERE p
#       UNION SELECT DISTINCT c FROM t WHERE NOT p
#       UNION SELECT DISTINCT c FROM t WHERE p IS NULL
#     ==  SELECT DISTINCT c FROM t
#
# This relation quietly depends on three separate operators agreeing about
# NULL. DISTINCT, UNION and GROUP BY all treat two NULLs as the same value,
# while `=` says NULL = NULL is NULL. If UNION used the `=` rule, a NULL
# from two arms would survive twice and the relation would be false for
# every table containing a NULL.
#
# GROUP BY. A group's rows are split across the three partitions, so each
# arm sees part of every group and the counts have to be re-aggregated per
# group. That is the COUNT-recombines-with-SUM rule from the aggregate
# table, now applied group by group:
#
#     SELECT g, SUM(n) FROM (
#         SELECT g, COUNT(*) AS n FROM t WHERE p GROUP BY g
#         UNION ALL SELECT g, COUNT(*) FROM t WHERE NOT p GROUP BY g
#         UNION ALL SELECT g, COUNT(*) FROM t WHERE p IS NULL GROUP BY g
#     ) GROUP BY g
#     ==  SELECT g, COUNT(*) FROM t GROUP BY g
#
# Here the inner composition MUST be UNION ALL -- the three partial counts
# for one group are the whole point and a set union would collapse two
# arms that happened to count the same number of rows.
#
# So the two relations need opposite composition rules, and each one is a
# false-alarm generator in the other's place. That is measured in the
# suite rather than asserted.


def distinct_check(db, pred, col, combine="UNION"):
    """Distinct values of a column, against the same set recombined."""
    whole = rows_of(db, "SELECT DISTINCT %s FROM t" % col)
    arms = (" %s " % combine).join(
        "SELECT DISTINCT %s FROM t WHERE %s" % (col, arm)
        for arm in ("(%s)" % pred, "NOT (%s)" % pred, "(%s) IS NULL" % pred))
    parts = rows_of(db, arms)
    if whole == parts:
        return None
    return ("DISTINCT", len(whole), len(parts))


def group_check(db, pred, col, outer="SUM"):
    """Per-group counts over the whole table, against them recombined."""
    whole = rows_of(db, "SELECT %s, COUNT(*) FROM t GROUP BY %s" % (col, col))
    parts = rows_of(db,
                    "SELECT %s, %s(n) FROM ("
                    "  SELECT %s, COUNT(*) AS n FROM t WHERE (%s) GROUP BY %s"
                    "  UNION ALL SELECT %s, COUNT(*) FROM t WHERE NOT (%s) GROUP BY %s"
                    "  UNION ALL SELECT %s, COUNT(*) FROM t WHERE (%s) IS NULL GROUP BY %s"
                    ") GROUP BY %s"
                    % (col, outer, col, pred, col, col, pred, col,
                       col, pred, col, col))
    if whole == parts:
        return None
    return ("GROUP BY", len(whole), len(parts))


def cmd_distinct(args):
    return run_oracle(args, lambda db, pred: distinct_check(db, pred, "c0"),
                      "DISTINCT")


def cmd_group(args):
    return run_oracle(args, lambda db, pred: group_check(db, pred, "c0"),
                      "GROUP BY")


# ------------------------------------------------------------- selftest
#
# Both oracles report nothing against SQLite, and will keep reporting
# nothing, because SQLite is among the most heavily tested software there
# is and the published runs of these techniques already took what was
# there. So the number they print is worth exactly as much as the evidence
# that they WOULD print something else.
#
# That evidence has to be manufactured, because the engine cannot be made
# to misbehave on request. These wrappers return wrong answers on purpose,
# each imitating a real optimiser bug class, and the selftest asserts every
# one is caught. An oracle that has never been watched to fail is an oracle
# that always passes.


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Corrupt:
    """A connection that is wrong in one specific, plausible way."""

    def __init__(self, db, kind):
        self.db = db
        self.kind = kind

    def execute(self, sql, *a):
        rows = self.db.execute(sql, *a).fetchall()
        return _Rows(self._spoil(sql, rows))

    def executemany(self, sql, *a):
        return self.db.executemany(sql, *a)

    def _spoil(self, sql, rows):
        k = self.kind
        # A row lost on one path. The classic shape: an index scan that
        # skips an entry, so the partitions no longer reassemble.
        if k == "drop-row" and "UNION ALL" in sql and len(rows) > 3:
            return rows[:-1]
        # A row returned twice, which is the same bug wearing a hat.
        if k == "duplicate-row" and "UNION ALL" in sql and rows:
            return rows + rows[:1]
        # Wrong only when the PREDICATE mentions NULL. Narrow bugs are the
        # realistic ones, and a fuzzer that only catches broad ones is not
        # worth much.
        #
        # Testing `"IS NULL" in sql` does not express that: the partition
        # query ends in `(p) IS NULL` whatever p is, so the condition was
        # true every single time and the "narrow" bug was as broad as the
        # others. The predicate appears three times in that query and the
        # third arm contributes one more occurrence, so more than one means
        # it came from the predicate.
        if k == "null-only" and "UNION ALL" in sql and sql.count("IS NULL") > 1 \
                and len(rows) > 2:
            return rows[:-1]
        # NoREC: the indexed count is off by one. Invisible to TLP, because
        # TLP never asks for a count.
        if k == "count-off-by-one" and sql.startswith("SELECT COUNT(*)") and rows:
            return [(rows[0][0] + 1,)]
        # An aggregate over the whole table that is wrong while every row is
        # present and correct. Neither row-level TLP nor NoREC can see this:
        # the rows reassemble perfectly and the counts agree. Only the
        # aggregate form asks the question it answers wrongly.
        #
        # "FROM (" excludes NoREC's own unoptimised query, which is also a
        # SELECT SUM(...) and was being corrupted by the first version of
        # this condition -- so the bug appeared to be caught by NoREC 100%
        # of the time, when what NoREC had actually noticed was this
        # wrapper breaking NoREC.
        if k == "aggregate-skew" and sql.startswith("SELECT SUM(") \
                and "FROM (" not in sql and "UNION" not in sql \
                and rows and rows[0][0] is not None:
            return [(rows[0][0] + 1,)]
        # A pair lost from the ON-partitioning. Only the inner-join
        # relation looks at those rows.
        if k == "join-drop-pair" and "JOIN t2 ON" in sql and "UNION ALL" in sql \
                and len(rows) > 3:
            return rows[:-1]
        # An outer join that pads one row too many. The inner-join relation
        # never issues a LEFT JOIN, so it cannot see this at all.
        if k == "leftjoin-overpad" and "LEFT JOIN" in sql and rows \
                and rows[0][0] is not None:
            return [(rows[0][0] + 1,)]
        # The same lost row, but fired on ANY set operation rather than on
        # UNION ALL specifically. This bug exists to answer a question
        # about the table rather than to model a new defect.
        #
        # The DISTINCT relation reads 0% against `drop-row` above, and the
        # tempting reading is that it cannot see a lost row. That is not
        # what is being measured: `drop-row` is written to fire on
        # "UNION ALL" and the DISTINCT relation recombines with "UNION",
        # so the 0% is a property of the trigger string. Widening it to
        # "UNION" separates the two explanations, and the DISTINCT column
        # moves off zero -- so the relation sees lost rows fine, and it
        # was the bug that could not see the relation.
        if k == "setop-drop-row" and "UNION" in sql and len(rows) > 3:
            return rows[:-1]
        # A DISTINCT that returns a value twice: a dedup step skipped on one
        # path. No other relation here ever writes DISTINCT, so no other
        # relation can see it.
        #
        # "UNION" not in sql is the lesson from aggregate-skew applied
        # before making the mistake again: the recombined side is three
        # SELECT DISTINCTs joined by UNION, and corrupting that too would
        # have this bug "caught" by a relation that had only noticed the
        # wrapper breaking the relation.
        if k == "distinct-dup" and sql.startswith("SELECT DISTINCT") \
                and "UNION" not in sql and rows:
            return rows + rows[:1]
        # A group wrongly excluded by a HAVING filter. Only the HAVING
        # relation ever issues a HAVING clause, so only it can see this.
        #
        # This corrupts the RECOMBINED side rather than the whole, which is
        # the opposite of the other planted bugs and is correct here: the
        # whole query of that relation has no HAVING in it at all. The
        # defect being modelled is a group wrongly filtered out, and the
        # HAVING'd query is where that happens.
        if k == "having-drop-group" and "HAVING" in sql and len(rows) > 2:
            return rows[:-1]
        # One group counted one row too many. Invisible to everything that
        # does not group: the rows all reassemble, the whole-table count is
        # right, and the whole-table aggregate is right.
        if k == "group-skew" and "GROUP BY" in sql and "UNION" not in sql \
                and rows and rows[0][1] is not None:
            return [(rows[0][0], rows[0][1] + 1)] + rows[1:]
        return rows


BUG_KINDS = ["drop-row", "duplicate-row", "null-only", "count-off-by-one",
             "aggregate-skew", "distinct-dup", "group-skew",
             "setop-drop-row", "having-drop-group"]


JOIN_BUG_KINDS = ["join-drop-pair", "leftjoin-overpad"]


def cmd_joinselftest(args):
    """The same argument as `selftest`, for the two join relations.

    Kept separate rather than folded into that table because it would not
    be an honest comparison: the single-table oracles query `t` and these
    query `t1` and `t2`, so "TLP does not catch this" would only mean the
    two were never pointed at the same thing. What is worth measuring is
    whether the two JOIN relations catch different bugs, and they do.
    """
    import random
    real = sqlite3.connect(":memory:")
    print("%-20s %10s %12s" % ("planted bug", "INNER", "LEFT JOIN"))
    missed = []
    for kind in JOIN_BUG_KINDS:
        hits = {"inner": 0, "left": 0}
        rnd = random.Random(args.seed)
        for _ in range(args.n):
            t1, t2 = build2(real, rnd, args.rows, args.null_rate)
            pred = gen_join_predicate(rnd, t1, t2, args.depth)
            bent = Corrupt(real, kind)
            for name, fn in (("inner", join_check), ("left", leftjoin_check)):
                try:
                    if fn(bent, pred):
                        hits[name] += 1
                except sqlite3.Error:
                    pass
        print("%-20s %9d%% %11d%%" % (kind,
                                      100 * hits["inner"] // args.n,
                                      100 * hits["left"] // args.n))
        if max(hits.values()) == 0:
            missed.append(kind)
    if missed:
        print("\nNOT CAUGHT: %s" % ", ".join(missed))
        return 1
    print("\nboth planted join bugs are caught, and by different relations")
    return 0


def cmd_selftest(args):
    import random
    real = sqlite3.connect(":memory:")

    print("%-18s %6s %6s %6s %9s %6s %7s"
          % ("planted bug", "TLP", "NoREC", "agg", "DISTINCT", "GROUP",
             "HAVING"))
    worst = {}
    for kind in BUG_KINDS:
        hits = dict.fromkeys(("TLP", "NoREC", "agg", "DISTINCT", "GROUP",
                              "HAVING"), 0)
        rnd = random.Random(args.seed)
        for _ in range(args.n):
            _, cols, _ = build(real, rnd, args.rows, args.null_rate)
            real.execute("CREATE INDEX IF NOT EXISTS i0 ON t(%s)" % cols[0][0])
            pred = gen_predicate(rnd, cols, args.depth)
            bent = Corrupt(real, kind)
            ints = [c for c, ty in cols if ty == "INTEGER"]
            for name, fn in (("TLP", tlp_check), ("NoREC", norec_check)):
                try:
                    if fn(bent, pred):
                        hits[name] += 1
                except sqlite3.Error:
                    pass
            if ints:
                try:
                    if agg_check(bent, pred, "SUM", ints[0]):
                        hits["agg"] += 1
                except sqlite3.Error:
                    pass
            for name, fn in (("DISTINCT", distinct_check), ("GROUP", group_check)):
                try:
                    if fn(bent, pred, cols[0][0]):
                        hits[name] += 1
                except sqlite3.Error:
                    pass
            # HAVING needs its own predicate: a row-level one is not legal
            # in a HAVING clause, and feeding it one would skip every check
            # with an sqlite3.Error and print a column of zeros.
            try:
                if having_check(bent, gen_having(rnd, cols, args.depth),
                                cols[0][0]):
                    hits["HAVING"] += 1
            except sqlite3.Error:
                pass
        print("%-18s %5d%% %5d%% %5d%% %8d%% %5d%% %6d%%"
              % (kind,
                 100 * hits["TLP"] // args.n,
                 100 * hits["NoREC"] // args.n,
                 100 * hits["agg"] // args.n,
                 100 * hits["DISTINCT"] // args.n,
                 100 * hits["GROUP"] // args.n,
                 100 * hits["HAVING"] // args.n))
        worst[kind] = max(hits.values())

    missed = [k for k, v in worst.items() if v == 0]
    if missed:
        print("\nNOT CAUGHT BY EITHER ORACLE: %s" % ", ".join(missed))
        return 1
    print("\nevery planted bug is caught by at least one oracle")
    return 0


def cmd_truth(args):
    db = sqlite3.connect(":memory:")
    exprs = enumerate_exprs(args.depth)
    bad = 0
    for sql, want in exprs:
        got = sql_value(db, sql)
        if got is not want:
            bad += 1
            if bad <= 10:
                print("MISMATCH %s -> sqlite %r, standard %r" % (sql, got, want))
    print("%d expressions, %d disagree with the standard (depth %d)"
          % (len(exprs), bad, args.depth))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(prog="tlp", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("truth", help="exhaustive three-valued logic check")
    p.add_argument("--depth", type=int, default=2)
    p.set_defaults(fn=cmd_truth)

    p = sub.add_parser("vacuity", help="how often the generator is interesting")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.set_defaults(fn=cmd_vacuity)

    for name, fn, helptext in (("tlp", cmd_tlp, "ternary partitioning"),
                               ("norec", cmd_norec, "optimised vs unoptimised"),
                               ("distinct", cmd_distinct, "partition a DISTINCT"),
                               ("group", cmd_group, "partition a GROUP BY")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--n", type=int, default=500)
        q.add_argument("--seed", type=int, default=0)
        q.add_argument("--rows", type=int, default=8)
        q.add_argument("--depth", type=int, default=2)
        q.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
        q.add_argument("--index", action="store_true", default=True)
        q.add_argument("--no-index", action="store_false", dest="index")
        q.add_argument("--max-reports", type=int, default=3, dest="max_reports")
        q.set_defaults(fn=fn)

    p = sub.add_parser("reduce", help="find a case against a planted bug and shrink it")
    p.add_argument("--bug", default="drop-row", choices=BUG_KINDS)
    p.add_argument("--tries", type=int, default=2000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=10)
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.set_defaults(fn=cmd_reduce)

    p = sub.add_parser("having", help="partition a HAVING clause")
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--depth", type=int, default=2)
    # 0.40, not the 0.25 every other command uses, and measured rather than
    # inherited. A HAVING partition splits GROUPS, and a group goes NULL
    # only when every value in it is NULL, which needs more NULLs than
    # making a single row's predicate go NULL does.
    p.add_argument("--null-rate", type=float, default=0.40, dest="null_rate")
    p.add_argument("--max-reports", type=int, default=3, dest="max_reports")
    p.set_defaults(fn=cmd_having)

    p = sub.add_parser("join", help="partition a join's ON clause")
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=6)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.add_argument("--index", action="store_true", default=True)
    p.add_argument("--no-index", action="store_false", dest="index")
    p.add_argument("--max-reports", type=int, default=3, dest="max_reports")
    p.set_defaults(fn=cmd_join)

    p = sub.add_parser("agg", help="aggregate partitioning")
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.add_argument("--max-reports", type=int, default=3, dest="max_reports")
    p.set_defaults(fn=cmd_agg)

    p = sub.add_parser("joinselftest", help="plant join bugs, check the relations notice")
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=6)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.set_defaults(fn=cmd_joinselftest)

    p = sub.add_parser("selftest", help="plant bugs and check the oracles notice")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.set_defaults(fn=cmd_selftest)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
