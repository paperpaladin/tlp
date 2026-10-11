# tlp

Finds queries where SQLite returns the **wrong rows**. Not a crash, not an
error -- a result that differs from what the data says it should be, with
nothing obvious to compare against.

```
$ python3 tlp.py tlp --n 500
TLP: 500 queries, 0 disagreements, 0 skipped, 152 genuinely three-valued (30%)

$ python3 tlp.py selftest
planted bug               TLP    NoREC
drop-row                 100%       0%
duplicate-row            100%       0%
null-only                 23%       0%
count-off-by-one           0%     100%

every planted bug is caught by at least one oracle
```

## Why there is no reference implementation

Wrong-answer bugs are the hard class precisely because nothing disagrees
with them. A fuzzer that looks for crashes has an obvious oracle: the
process died. A fuzzer that looks for wrong answers needs to know what the
right answer was, and if you could compute that you would not need the
database.

The way out is a **metamorphic** oracle: a relation the engine must satisfy
with *itself*.

SQL is three-valued. For any predicate `p`, every row of `t` makes `p`
exactly one of TRUE, FALSE or NULL -- never two, never none. So the three
partitions must reassemble into the whole:

```sql
SELECT * FROM t WHERE p
  UNION ALL SELECT * FROM t WHERE NOT p
  UNION ALL SELECT * FROM t WHERE p IS NULL
```

must be the same multiset as `SELECT * FROM t`. Always. Any predicate, any
data, any engine.

That is **TLP**, ternary logic partitioning (Rigger and Su, OOPSLA 2020).
It needs no second implementation, no stored expectation, and no model of
what the query means. It cannot be satisfied by being self-consistently
wrong, which is the failure mode a hand-written expected output has.

**NoREC** (Rigger and Su, ESEC/FSE 2020) attacks from the other side. The
same predicate in a `WHERE` clause can be answered from an index; in the
select list of a subquery it cannot, because every row has to be fetched
and evaluated:

```sql
SELECT COUNT(*) FROM t WHERE p
SELECT SUM(w) FROM (SELECT ((p) IS TRUE) AS w FROM t)
```

When those disagree it is the index path that is wrong -- the half no
amount of reading the query will show you.

Between them these two techniques found over 400 real bugs in SQLite,
MySQL, PostgreSQL and TiDB.

## Neither oracle subsumes the other

That is a design claim, so it is measured rather than asserted. From the
table above: TLP catches every row-level bug and **none** of the count bug;
NoREC catches the count bug and **none** of the row-level ones. TLP never
asks for a count, so it cannot see a count that is wrong; NoREC only ever
compares two numbers, so it cannot see which rows moved.

## The generator decides whether any of this means anything

TLP splits rows three ways. A predicate that is never NULL leaves the third
arm empty, and the relation degenerates into *"the engine agrees with
itself about TRUE and FALSE"* -- which it will, forever, while printing a
large and encouraging number.

So the fraction of predicates that are genuinely three-valued is measured,
not assumed, and the suite holds it above a floor:

```
$ python3 tlp.py vacuity --n 600
600 predicates over 8-row tables, NULL rate 0.25
   78.0%  have a non-empty NULL partition
   31.5%  are genuinely three-valued (all three non-empty)
   12.3%  are constant across every row (partition nothing)
```

Two generator decisions do all the work, and both are about making
comparisons **collide**. The value domain is tiny -- integers 0..4, three
short strings -- because random 64-bit integers would make `a = b` false
for every pair of rows that ever existed. And NULLs are frequent by
construction rather than by accident.

The NULL rate has an optimum, which is not obvious until it is measured:

| NULL rate | three-valued | partition nothing |
|---|---|---|
| 0.00 | 2% | 53% |
| 0.15 | 27% | 24% |
| **0.25** | **32%** | **13%** |
| 0.45 | 27% | 8% |
| 0.60 | 22% | 13% |

Too few NULLs and the third partition is empty. Too many and the rows are
mostly NULL, so the *other* two empty out instead and the predicate becomes
constant. At 0 it is 2% rather than 0% only because a literal `NULL` can
still appear inside the predicate itself.

## What does not work

**It has never found a bug in SQLite, and probably will not.** SQLite is
among the most heavily tested software in existence, and the published runs
of these exact techniques already took what was there. A run reporting
nothing is the expected outcome. What this has to prove is that it *would*
report something, which is what `selftest` is for and why the planted-bug
table is the first thing in this file.

No other engines. There is no DuckDB or PostgreSQL on this machine -- which
is the reason for choosing a metamorphic oracle over differential testing,
rather than a limitation of it.

No floating point, no collation-dependent operators, no `LIKE`, no
triggers, views, foreign keys, virtual tables or window functions. Row
order is never compared, only sorted multisets, because SQL does not
promise an order without `ORDER BY` and comparing sequences would report a
bug every time the planner legitimately changed its mind.

Only `WHERE` is partitioned so far. The published work found the most bugs
partitioning a **JOIN's `ON` clause** and in the aggregate variants
(`SUM`, `COUNT`, `MIN`, `MAX` over the three partitions, each with its own
composition rule). Those are the next items in `PLAN.md` and they are where
this gets genuinely interesting.

## The one hand-written thing, and why

`tlp.py` transcribes the SQL standard's three-valued truth tables, and
checks all 34,353 expressions of the grammar up to depth 2 against SQLite.

That is the only place here where an expected answer is written down by
hand, which is the thing this project otherwise refuses to do, so it is
labelled a **calibration** rather than an oracle. It exists to prove the
model of three-valued logic the generator rests on matches the engine
before anything is built on it. The fuzzer's oracle is the metamorphic
relation, which needs no tables at all.

And because a conformance check that passes on its first run has not been
shown to work, the suite breaks it four ways -- an `AND` where FALSE does
not dominate, an `OR` where TRUE does not, a `NOT` that turns NULL into
FALSE, an `=` that does not propagate NULL -- and asserts it notices each.

A fifth rewrite survives and is recorded as surviving: changing `a is b` to
`a == b` in the `IS` table changes nothing, because on the domain
`{True, False, None}` Python's two operators agree everywhere. An
equivalent mutant rather than a hole.

## Tests

```
$ make test
21 passed, 0 failed
```

No pytest, no network, no dependencies. `sqlite3` is in the standard
library, which is the whole toolchain.

## License

GPL-3.0-or-later.
