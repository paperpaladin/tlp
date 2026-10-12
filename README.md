# tlp

Finds queries where SQLite returns the **wrong rows**. Not a crash, not an
error -- a result that differs from what the data says it should be, with
nothing obvious to compare against.

```
$ python3 tlp.py tlp --n 500
TLP: 500 queries, 0 disagreements, 0 skipped, 150 genuinely three-valued (30%)

$ python3 tlp.py selftest
planted bug           TLP  NoREC   agg  DISTINCT  GROUP  HAVING   LIKE
drop-row             100%     0%    0%        0%    79%     79%     0%
duplicate-row        100%     0%    0%        0%   100%    100%     0%
null-only             24%     0%    0%        0%    24%     31%     0%
count-off-by-one       0%   100%    0%        0%     0%      0%     0%
aggregate-skew         0%     0%   85%        0%     0%      0%     0%
distinct-dup           0%     0%    0%      100%     0%      0%     0%
group-skew             0%     0%    0%        0%   100%    100%     0%
setop-drop-row       100%     0%    0%       79%    79%     79%     0%
having-drop-group      0%     0%    0%        0%     0%     97%     0%
glob-skew              0%     0%    0%        0%     0%      0%    27%
groupfilter-skew       0%     0%    0%        0%   100%      0%     0%

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

**Aggregate TLP** is the third, and the one where the technique stops
being a one-liner. An aggregate over the whole table must equal the same
aggregate recombined from the three partitions -- but there is no single
recombination rule:

| aggregate | recombine with | why |
|---|---|---|
| `SUM` | `SUM` | sums add; `SUM` of nothing is NULL and the outer `SUM` skips it |
| `COUNT` | **`SUM`** | counts **add**, they do not count |
| `MIN` | `MIN` | `MIN` ignores NULLs at both levels, which is what makes them agree |
| `MAX` | `MAX` | the mirror of `MIN` |
| `AVG` | *(none)* | the average of three averages is not the average |

## No oracle subsumes another

That is a design claim, so it is measured rather than asserted, and the
table above is the measurement. Each oracle catches exactly the class it
was built for and **nothing else**:

- TLP never asks for a count, so it cannot see a count that is wrong.
- NoREC only ever compares two numbers, so it cannot see which rows moved.
- Both compare rows or counts, so neither can see an aggregate that is
  wrong while every row is present and every count agrees.
- None of the three writes `DISTINCT`, so a dedup step skipped on one path
  is invisible to all of them.
- None of the four groups, so one group counted wrong -- with every row
  present, every count right and every whole-table aggregate right -- is
  invisible to all of them.
- None of the five writes a `HAVING` clause, so a group wrongly filtered
  out by one is invisible to all of them.

### And the matrix itself was missing four relations

The check above covers the seven single-table relations. It did not cover
the four join relations, while the module docstring claimed *"eleven
relations, and none of them is redundant: for every pair there is a planted
bug one catches and the other does not"*.

That was false. Run over the join half, the same algorithm reports
**`join3 is dominated by inner`** -- the three-table `ON`-partitioning
caught nothing the two-table one did not, visible in the table above as
`48%` sitting under `100%` in the only row either of them scored on. The
identical hole this section congratulates itself on closing for `GROUP`
against `HAVING`, one table lower down, with the claim stated for eleven
relations and asserted for seven.

`join3-drop-pair` is the bug that separates them: a pair lost from a
`UNION ALL` partition query that mentions `t3`, which is a shape no other
relation here emits -- the inner and left relations never join `t3`, and the
reordering relation never partitions. `JOIN3` catches it 48% of the time and
the other three 0%.

The matrix now runs over both halves. Deleting that one bug turns it red
with `join3 is dominated by inner`, which is how the hole was found in the
first place -- by an audit that ran the check over relations the check did
not reach.

### The claim is checked as a matrix, because the prose missed something

Arguing non-subsumption one pair at a time, from the shape of the queries,
is right but it is not a measurement -- and it missed a case.

**The `GROUP BY` column used to be dominated by the `HAVING` column**: the
same percentage or lower on every single planted bug. Nothing in the
reasoning above was wrong. In principle neither subsumes the other, because
`GROUP BY` partitions a `WHERE` clause *underneath* a grouping and
re-aggregates the per-arm counts, while `HAVING` partitions the `HAVING`
and re-aggregates nothing. What the table actually showed was that **no
planted bug modelled a defect in the path `GROUP BY` alone covers** -- so
on the available evidence, carrying it was pointless.

`groupfilter-skew` is that bug: a per-partition grouped count that is
wrong, which is grouping gone wrong under a filter. `GROUP BY` catches it
100% of the time and `HAVING` 0%, and the last row of the table above is
the two relations finally being told apart.

The suite now checks the whole claim pairwise rather than in prose: for
every ordered pair of relations there must be some planted bug the first
catches and the second does not. Deleting `groupfilter-skew` turns it red
with `GROUP is dominated by HAVING`, which is how the hole was found.

### One zero in that table is not what it looks like

The `DISTINCT` column reads 0% against `drop-row`, and the tempting
reading is that the relation cannot see a lost row. It cannot be read that
way, because `drop-row` is *written* to fire on `UNION ALL` and the
`DISTINCT` relation recombines with `UNION`. The 0% is a property of the
planted bug's trigger, not of the relation.

Separating the two explanations costs one more planted bug. `setop-drop-row`
is the same lost row fired on any set operation, and the `DISTINCT` column
moves to 79% -- so the relation sees lost rows fine, and it was the bug
that could not see the relation. The last row of the table exists only to
answer that question, and without it the table would be quietly overclaiming.

### The composition rules are load-bearing

The table of rules above looks arbitrary until you use the wrong one.
Against a database that is behaving perfectly:

| aggregate | correct rule | wrong rule | wrong rule |
|---|---|---|---|
| `SUM` | 0 / 174 | `MIN` 135 | `COUNT` 174 |
| `COUNT` | 0 / 210 | `COUNT` 207 | `MIN` 210 |
| `MIN` | 0 / 210 | `SUM` 176 | `MAX` 111 |
| `MAX` | 0 / 210 | `SUM` 178 | `MIN` 107 |

Every correct rule raises **no** false alarms; every wrong one raises
hundreds. `COUNT` is the one to stare at: its combiner is `SUM`, and
writing `COUNT` there by analogy with the others -- the obvious mistake --
produces a tool that reports a bug in 207 of 210 correct queries. (`SUM`
runs over fewer checks than the rest because it needs an integer column
and the generator does not always produce one.)

## DISTINCT and GROUP BY need opposite composition rules

Each of these is the partitioning done over a query shape where the obvious
recombination is the wrong one -- the same trap as the aggregate table, and
worth meeting twice because the two traps point in opposite directions.

**`DISTINCT` recombines with `UNION`**, a set union, and it is the only
relation here that does not use `UNION ALL`. The three partitions still
cover every row, so every distinct value appears in at least one arm -- but
a value can appear in *two*, because two rows sharing a value can land on
opposite sides of the predicate:

```sql
  SELECT DISTINCT c FROM t WHERE p
    UNION SELECT DISTINCT c FROM t WHERE NOT p
    UNION SELECT DISTINCT c FROM t WHERE p IS NULL
==  SELECT DISTINCT c FROM t
```

**`GROUP BY` must recombine with `UNION ALL` inside**, for exactly the
reason `DISTINCT` must not. A group's rows are split across the three
partitions, so each arm holds part of every group, and the three partial
counts are the entire point -- a set union would collapse two arms that
happened to count the same number of rows. Then the counts are
re-aggregated per group, which is the `COUNT`-recombines-with-`SUM` rule
from the aggregate table applied group by group:

```sql
  SELECT g, SUM(n) FROM (
      SELECT g, COUNT(*) AS n FROM t WHERE p GROUP BY g
      UNION ALL SELECT g, COUNT(*) FROM t WHERE NOT p GROUP BY g
      UNION ALL SELECT g, COUNT(*) FROM t WHERE p IS NULL GROUP BY g
  ) GROUP BY g
==  SELECT g, COUNT(*) FROM t GROUP BY g
```

So each relation's rule is a false-alarm generator in the other's place,
measured against a database that is behaving correctly:

| `DISTINCT` recombined with | false alarms | | `GROUP BY` counts recombined with | false alarms |
|---|---|---|---|---|
| **`UNION`** | **0 / 200** | | **`SUM`** | **0 / 200** |
| `UNION ALL` | 109 | | `COUNT` | 198 |
| `INTERSECT` | 200 | | `MIN` | 109 |
| `EXCEPT` | 192 | | `MAX` | 109 |
| | | | `AVG` | 109 |

The `109` is the same number four times over, and it is the same event seen
from two sides: one value of the column landing in more than one partition.
`UNION ALL` then keeps it twice, and the per-arm counts then sum to more
than any single one of them. Measured directly -- a value spans partitions
in exactly that many of the 200 cases -- rather than left as a coincidence,
and the suite asserts the two counts are equal rather than merely similar.

### A hidden dependency on three operators agreeing about NULL

The `DISTINCT` relation quietly rests on something the SQL standard makes
easy to miss. `DISTINCT`, `UNION` and `GROUP BY` all treat two NULLs as the
*same* value, while `=` says `NULL = NULL` is NULL. If `UNION` followed the
`=` rule, a NULL arriving from two arms would survive twice and the
relation would be false for every table containing one. The suite checks
all three operators directly rather than trusting that reading of the
standard.

## HAVING partitions groups, so the rule is different a third time

A `WHERE` predicate decides which **rows** survive. An `ON` predicate
decides which **pairs** exist. A `HAVING` predicate decides which
**groups** survive -- and the groups are formed by the `GROUP BY` before
`HAVING` ever runs. So every group makes the predicate exactly one of TRUE,
FALSE or NULL, and partitioning it splits the groups three ways while
leaving every group itself untouched.

That makes the recombination the **simplest** one here, in the exact place
where `GROUP BY` needed the most care:

```sql
  SELECT g, COUNT(*) FROM t GROUP BY g HAVING h
    UNION ALL SELECT g, COUNT(*) FROM t GROUP BY g HAVING NOT h
    UNION ALL SELECT g, COUNT(*) FROM t GROUP BY g HAVING h IS NULL
==  SELECT g, COUNT(*) FROM t GROUP BY g
```

Plain `UNION ALL`, no re-aggregation. The `GROUP BY` relation needed
`SUM(n)` over its arms because partitioning a `WHERE` clause cuts every
group into three pieces that have to be added back. Partitioning a `HAVING`
clause cuts no group at all: each one arrives whole in exactly one arm,
already counted.

### So what is load-bearing moves

For `DISTINCT` and `GROUP BY` the composition operator was the trap. Here
it is not a trap at all, and the traps are all in the arms:

| variation | false alarms in 200 |
|---|---|
| as shipped | **0** |
| `UNION` instead of `UNION ALL` | **0** -- an equivalent rewrite |
| drop the `IS NULL` arm | 88 |
| `IS NOT TRUE` alongside an `IS NULL` arm | 88 |
| a duplicated TRUE arm | 136 |

`UNION` costs nothing here because each arm emits at most one row per group
key and the key is unique within a group, so the recombined query never
contains two identical rows and a set union has nothing to collapse. That
is recorded rather than relied on quietly -- it is the one place in this
project where the obvious-looking mistake is not a mistake.

### The three-way split is a choice, not a correctness requirement

The fourth row of that table is the one that changed what I thought this
project was doing. `h IS NOT TRUE` is exactly FALSE-or-NULL, so **two**
arms already cover every group once:

| | false alarms in 200 |
|---|---|
| `h` / `NOT h` / `h IS NULL` | 0 |
| `h` / `h IS NOT TRUE` | 0 |

Both are sound. And it is not a quirk of `HAVING` -- the same two-arm split
holds for the row-level relation this project is named after, checked
directly over 200 queries. "Ternary" describes how the technique is usually
written, not a property it needs in order to be valid.

The obvious next claim is that three arms is nonetheless the **stronger**
test, because it issues a third query shape the engine may plan
differently. That claim is not supported by anything measured here:

| planted bug | three arms | two arms |
|---|---|---|
| `drop-row` | 100% | 100% |
| `duplicate-row` | 100% | 100% |
| `null-only` | 21% | 21% |
| `setop-drop-row` | 100% | 100% |

Identical. Which is a finding about the **planted bugs** rather than about
the relations: these wrappers corrupt result sets, not query plans, so they
cannot tell two sound relations apart no matter how differently a real
engine would execute them. That is the ceiling of this whole selftest
approach and it is worth stating plainly, because the planted-bug table is
the first thing in this file and it would otherwise read as measuring more
than it does.

### The generator is where the difficulty went instead

A HAVING predicate is built from aggregates, and aggregates go NULL in a
way columns do not: `SUM`, `MIN` and `MAX` of a group whose values are all
NULL are NULL, while `COUNT(*)` and `COUNT(c)` never are. So the mix of
aggregates decides whether the third arm exists at all:

| `COUNT` terms | NULL rate | three-valued | has a NULL arm | partitions nothing |
|---|---|---|---|---|
| 0.10 | 0.25 | 15% | 32% | 43% |
| **0.10** | **0.40** | **18%** | **42%** | **33%** |
| 0.10 | 0.55 | 15% | 48% | 32% |
| 0.25 | 0.40 | 14% | 35% | 38% |
| all `COUNT` | any | 0% | 0% | -- |

Same interior optimum as the row-level NULL rate, and for the same reason:
more NULLs fill the third arm but empty the other two. `having` is the one
command whose NULL-rate default is 0.40 rather than 0.25, because a group
goes NULL only when **every** value in it is NULL, which takes more NULLs
than making one row's predicate go NULL.

18% is far below the row-level 29%, and the reason is structural rather
than fixable: an eight-row table has about **3.9 groups**, and splitting
four things three ways without leaving an arm empty is simply harder than
splitting eight. The right comparison for this number is not the row-level
one.

## Partitioning a join's ON clause

The published work found more bugs here than anywhere else, and the reason
is worth stating: a `WHERE` predicate is evaluated once per row, but an
`ON` predicate decides which **pairs** of rows exist at all. Getting it
wrong changes the shape of the result rather than its contents, and join
reordering, index selection on the inner side and the outer-join padding
rules all meet in that one clause.

Every pair in the cross product makes the predicate exactly one of TRUE,
FALSE or NULL, and an inner join keeps precisely the TRUE pairs -- so the
three `ON`-partitions are a partition of the cross product:

```sql
  t1 JOIN t2 ON p
    UNION ALL t1 JOIN t2 ON NOT p
    UNION ALL t1 JOIN t2 ON p IS NULL
==  t1, t2
```

**The same trick does not work for a `LEFT JOIN`**, which is the part worth
getting right rather than assuming. Each arm pads its own unmatched rows,
so the padding is counted three times over. What is true instead is a
decomposition -- a left join is its inner join plus one row for every left
row that found no TRUE match anywhere:

```sql
  COUNT(t1 LEFT JOIN t2 ON p)
==  COUNT(t1 JOIN t2 ON p)
  + COUNT(t1 WHERE NOT EXISTS (SELECT 1 FROM t2 WHERE p))
```

That is a different shape of query from anything else here: `NOT EXISTS`
is a correlated subquery, and the optimiser rewrites it into something else
entirely.

```
$ python3 tlp.py joinselftest
planted bug             INNER  LEFT JOIN    JOIN3   REORDER
join-drop-pair           100%         0%      48%        0%
leftjoin-overpad           0%       100%       0%        0%
reorder-skew               0%         0%       0%       30%
join3-drop-pair            0%         0%      48%        0%

every planted join bug is caught, and by different relations
```

Kept as a separate table from the one above rather than folded into it,
because that would not be an honest comparison: the single-table oracles
query `t` and these query `t1`, `t2` and `t3`, so "TLP does not catch this"
would only mean the two were never pointed at the same thing. The four join
relations *are* all pointed at the same three tables, so that table is a
real comparison between them.

Every generated `ON` predicate is forced to mention both tables. Without
that it is a filter on one side, the planner pushes it down, and the join
being tested is not the join it appears to be. 65% of them genuinely split
the cross product rather than keeping all of it or none.

## A third table, so the planner finally has a choice

Two tables give a planner one join and two orders to do it in. Three give
it a real choice, and **reordering is where a planner has the most freedom
to be wrong** -- which is why, for as long as only two tables existed here,
this file had to admit that the most interesting thing about joins was
barely being exercised.

The `ON`-partitioning carries over directly, with the left side of the
partitioned join now a join itself:

```sql
  t1 JOIN t2 ON p12 JOIN t3 ON p
    UNION ALL t1 JOIN t2 ON p12 JOIN t3 ON NOT p
    UNION ALL t1 JOIN t2 ON p12 JOIN t3 ON p IS NULL
==  t1 JOIN t2 ON p12 JOIN t3 ON 1
```

The new relation is the one that could not exist with two tables. SQLite
documents `CROSS JOIN` as **suppressing its own reordering**: the tables
are visited in the order written. So the same query written both ways must
return the same multiset, and the planner's choice is being differenced
against a fixed order:

```sql
    SELECT * FROM t1 JOIN t2 JOIN t3 WHERE p
==  SELECT * FROM t1 CROSS JOIN t2 CROSS JOIN t3 WHERE p
```

The two forms reach genuinely different machinery. Asked what it decided,
SQLite answers:

```
  free  : SCAN t1 | SCAN t3 | BLOOM FILTER ON t2 (b1=?) | SEARCH t2 USING AUTOMATIC COVERING INDEX (b1=?)
  forced: SCAN t1 | BLOOM FILTER ON t2 (b1=?) | SEARCH t2 USING AUTOMATIC COVERING INDEX (b1=?) | SCAN t3
```

Different visit order, and an automatic covering index and a Bloom filter
built at a different point in the plan.

### The relation is worth nothing when the plans agree

In the runs where SQLite picks the same order both ways, this compares a
query against itself. So the fraction where it actually reordered is
measured and reported, the same way the three-valued fraction is:

```
$ python3 tlp.py join3 --n 400
three-table joins: 400 queries, 0 disagreements, 0 skipped,
                   247 where the planner chose a different order (62%)
```

And because the whole relation rests on `CROSS JOIN` pinning the order, the
suite asks `EXPLAIN QUERY PLAN` whether it really does, rather than taking
it from the documentation.

### A lever that did not survive being measured properly

This is worth recording because the mistake was mine and it passed a test.

A minimal probe -- three tables, two cross-table comparisons, nothing else
-- said that equality predicates and an index were what made the planner
reorder, moving the rate from **15% to 67%**. Both looked decisive, and the
generator was built around them.

On the shipped generator they are not decisive at all:

| | reordering rate |
|---|---|
| as shipped | ~60% |
| without the equality bias | 1 to 6 points lower |
| without the index | 6 to 11 points lower |
| without either | 15 to 19 points lower |

Both still point the same way on every seed tried, and together they are
worth something. But the 50-point effect the probe reported did not
transfer, because the real generator's predicates already carry several
cross-table comparisons and the planner has reasons to reorder with or
without the help.

The test written to assert the equality lever **passed on noise**: 58%
against 64% at 200 cases, which is 61% against 60% at 500. What is asserted
now is the combined effect, whose margin is 15 to 19 points; the individual
rates are printed without an assertion the sample size cannot support.

## Two pattern matchers, pointed at each other

This is the one differential oracle here rather than a metamorphic one,
and it still needs no second database. **SQLite ships two independent
pattern matchers.** `LIKE` and `GLOB` are separate implementations with
separate wildcard syntax, and over a restricted alphabet the two languages
say the same thing under `%` -> `*` and `_` -> `?`:

```sql
    SELECT * FROM t WHERE c LIKE 'a%b'
==  SELECT * FROM t WHERE c GLOB 'a*b'
```

```
$ python3 tlp.py like --n 400
LIKE vs GLOB: 339 queries, 0 disagreements, 61 skipped,
              112 where the pattern split the table (33%)
```

Row for row, `NULL` included -- where both must yield NULL rather than
FALSE. The vacuity question is different again here: a pattern matching no
row, or every row, makes the two matchers agree trivially, so what is
reported is how often the pattern actually split the table.

### Two preconditions, both enforced rather than hoped for

**`LIKE` folds ASCII case and `GLOB` never does.** Left alone, the two
disagree on the first capital letter. So `case_sensitive_like=ON` is pinned
in a single `connect()` that every connection here goes through, rather
than at each call site -- a connection that forgot it would not fail
loudly, it would quietly measure something else. The suite checks that a
connection *without* the pragma answers differently, which is the only way
to know the pinning does anything.

**The rewrite is only valid over an alphabet with no `GLOB`
metacharacters.** This is the part that would hold quietly and then not:

```
'axb' LIKE 'a*b'   is FALSE   -- * is a literal to LIKE
'axb' GLOB 'a*b'   is TRUE    -- * is a wildcard to GLOB
```

and the same for `?`, and for `[`, which opens a character class in `GLOB`
and is a literal in `LIKE`. Backslash is out too, because `LIKE` takes an
`ESCAPE` clause and `GLOB` has none. So generated patterns are built only
from `a`, `b`, `c`, `%` and `_`, `like_to_glob` *asserts* that rather than
trusting it, and the suite checks both that the assertion fires and that
translating such a pattern anyway really would change what is matched.

### It reaches the other oracles too

Pattern matching was added to the shared atom generator rather than bolted
on as a tenth relation, so `LIKE` and `GLOB` now appear inside the
predicates every other oracle partitions -- about 1 predicate in 4. That
moved every measured number in this file slightly, which is how the
README-consistency check in the suite earned its place: it went red on the
composition tables the moment the generator changed.

## Shrinking a case to something a human will read

A found case is a schema, ten rows and a predicate like
`((c1 IS NOT NULL OR c0 <= 2) AND NOT (c2 = c0 OR c3 < 'bb'))`. Nobody
reads that, so an unreduced report is the same as no report.

The shrink is greedy delta debugging over three axes -- drop a row, replace
a predicate with one of its own subtrees, pull a value toward NULL --
keeping any change that leaves the case still failing.

**The only property that matters is soundness: what comes out must still
fail.** A reducer that shrinks past the bug emits a minimal case that
reproduces nothing and sends whoever reads it hunting for a bug that is not
there, which is strictly worse than printing the unreduced version. Every
reduction asserts it on its own output:

```
$ python3 tlp.py reduce --bug null-only
found at try 2, against the planted bug 'null-only'
  rows       10 -> 3
  predicate  13 nodes -> 1

CREATE TABLE t (c0 INTEGER, c1 TEXT, c2 INTEGER, c3 TEXT);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL);
-- predicate: c0 IS NULL
-- still fails after reduction: True
```

That output is worth reading twice. `null-only` is the narrow planted bug:
it fires only above two rows and only when the predicate itself mentions
NULL. The reducer was told neither thing, and landed on exactly three rows
and exactly `c0 IS NULL`. It rediscovered the bug's trigger condition by
shrinking, which is what a reducer is for.

Because the bugs are planted, their true floors are known, so the suite can
assert the reducer reaches them rather than merely getting smaller:

| planted bug | fires when | floor | reached |
|---|---|---|---|
| `drop-row` | partition returns > 3 rows | 4 rows | 4 |
| `duplicate-row` | any non-empty result | 1 row | 1 |
| `null-only` | > 2 rows, NULL in the predicate | 3 rows | 3 |

"Smaller than before" would be satisfied by a reducer that drops one row
and gives up. Hitting the floor is a measurement.

### The shrink order has to be well-founded

The value axis looks like the easy one and is the only part that went
wrong. The first version offered every value NULL and 0 as candidates,
counted any accepted swap as progress, and **never terminated**: NULL
became 0, then 0 became NULL, forever, each step looking locally like an
improvement.

So candidates are now strictly below the current value on a total order --
NULL, then 0 or the empty string, then everything else -- and the suite
asserts no candidate leads back to the value it replaced. Deleting the
strictness makes the test run hang rather than fail, which is the honest
way for that mutation to show up.

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
   77.8%  have a non-empty NULL partition
   29.8%  are genuinely three-valued (all three non-empty)
   11.7%  are constant across every row (partition nothing)
```

Two generator decisions do all the work, and both are about making
comparisons **collide**. The value domain is tiny -- integers 0..4, three
short strings -- because random 64-bit integers would make `a = b` false
for every pair of rows that ever existed. And NULLs are frequent by
construction rather than by accident.

The NULL rate has an optimum, which is not obvious until it is measured:

| NULL rate | three-valued | has a NULL arm | partition nothing |
|---|---|---|---|
| 0.00 | 1% | 15% | 54% |
| 0.15 | 28% | 66% | 21% |
| **0.25** | **29%** | **78%** | **12%** |
| 0.45 | 26% | 88% | 5% |
| 0.60 | 18% | 87% | 11% |

Too few NULLs and the third partition is empty. Too many and the rows are
mostly NULL, so the *other* two empty out instead and the predicate becomes
constant. At 0 it is 1% rather than 0% only because a literal `NULL` can
still appear inside the predicate itself -- and the NULL-arm column shows
why that is not the same as 0%: even with no NULL in the data, 15% of
predicates still produce one.

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

No floating point, no collation-dependent operators, no triggers, views,
foreign keys, virtual tables or window functions. `LIKE` is in, but only
with its case folding pinned off and only over a five-character pattern
alphabet; `ESCAPE` clauses, character classes and `LIKE` on non-text
columns are all absent. Row
order is never compared, only sorted multisets, because SQL does not
promise an order without `ORDER BY` and comparing sequences would report a
bug every time the planner legitimately changed its mind.

The reducer shrinks rows, the predicate tree and values. It does not
shrink the SCHEMA -- an unused column stays in the `CREATE TABLE` even when
no surviving part of the predicate mentions it, which is visible in the
worked example above.

`GROUP BY` is partitioned only for `COUNT(*)`, and only on a single
grouping column.

**The planted bugs cannot distinguish two sound relations.** They wrap the
connection and corrupt result sets, so they model a wrong answer but never
a wrong plan -- which is why the two-arm and three-arm splits score
identically above. Every percentage in the first table is a measurement of
"would this relation notice a result that is wrong in this specific way",
and none of them is a measurement of how much of the optimiser gets
exercised.

Three tables at most, and the reordering relation only compares the
planner's order against the written one -- it never asks for a specific
*different* order, because SQLite has no hint for that beyond `CROSS JOIN`.

`reorder-skew`, the planted bug for it, is openly a **proxy**: SQLite
cannot be made to choose a wrong order on request, so what gets corrupted
is the `CROSS JOIN` form, whose order is pinned. The effect on the relation
matches a planner that got the free order wrong; the limitation is that a
real reordering bug would also change the plan, which this cannot.

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
130 passed, 0 failed
```

No pytest, no network, no dependencies. `sqlite3` is in the standard
library, which is the whole toolchain.

## License

GPL-3.0-or-later.
