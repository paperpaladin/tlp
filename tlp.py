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


def gen_predicate(rnd, cols, depth=2):
    if depth <= 0 or rnd.random() < 0.3:
        return gen_atom(rnd, cols)
    k = rnd.random()
    if k < 0.20:
        return "(NOT %s)" % gen_predicate(rnd, cols, depth - 1)
    op = "AND" if k < 0.60 else "OR"
    return "(%s %s %s)" % (gen_predicate(rnd, cols, depth - 1), op,
                           gen_predicate(rnd, cols, depth - 1))


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
                print("  INSERT INTO t VALUES %r;" % (r,))
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
                    print("  INSERT INTO t VALUES %r;" % (r,))
                print("  predicate: %s" % pred)
                print("  whole %r, recombined %r" % (bad[1], bad[2]))
                if found >= args.max_reports:
                    return 1

    print("aggregate TLP: %d checks, %d disagreements, %d skipped" %
          (tested, found, skipped))
    print("  per aggregate: %s" %
          ", ".join("%s %d" % (a, n) for a, n in sorted(per_agg.items())))
    return 1 if found else 0


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
        return rows


BUG_KINDS = ["drop-row", "duplicate-row", "null-only", "count-off-by-one",
             "aggregate-skew"]


def cmd_selftest(args):
    import random
    real = sqlite3.connect(":memory:")

    print("%-20s %8s %8s %8s" % ("planted bug", "TLP", "NoREC", "agg"))
    worst = {}
    for kind in BUG_KINDS:
        hits = {"TLP": 0, "NoREC": 0, "agg": 0}
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
        print("%-20s %7d%% %7d%% %7d%%" % (kind,
                                           100 * hits["TLP"] // args.n,
                                           100 * hits["NoREC"] // args.n,
                                           100 * hits["agg"] // args.n))
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
                               ("norec", cmd_norec, "optimised vs unoptimised")):
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

    p = sub.add_parser("agg", help="aggregate partitioning")
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rows", type=int, default=8)
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--null-rate", type=float, default=0.25, dest="null_rate")
    p.add_argument("--max-reports", type=int, default=3, dest="max_reports")
    p.set_defaults(fn=cmd_agg)

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
