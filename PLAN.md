# tlp -- plan

## Contract

`tlp` looks for queries where SQLite returns the **wrong rows**: a result
that differs from what the data says it should be, with no error raised and
nothing obvious to compare against.

```
tlp.py truth   [--depth N]           three-valued logic, exhaustively
tlp.py vacuity [--n N]               how often the generator is interesting
tlp.py tlp     [--n N] [--seed S]    ternary partitioning
tlp.py norec   [--n N] [--seed S]    optimised against unoptimised
tlp.py agg     [--n N] [--seed S]    aggregate partitioning
tlp.py join    [--n N] [--seed S]    partition a join's ON clause
tlp.py join3   [--n N] [--seed S]    three tables, and join reordering
tlp.py distinct [--n N] [--seed S]   partition a DISTINCT
tlp.py group   [--n N] [--seed S]    partition a GROUP BY
tlp.py having  [--n N] [--seed S]    partition a HAVING clause
tlp.py like    [--n N] [--seed S]    LIKE against GLOB
tlp.py selftest                      plant bugs, check the oracles notice
tlp.py joinselftest                  the same, for the join relations
tlp.py reduce  [--bug K]             shrink a found case to a minimal one
```

Exit status: 0 nothing found, 1 a disagreement was found, 2 usage error.

## Hard invariant

**Every disagreement reported is a real one.**

A tool that cries wolf about a database that is behaving correctly is worse
than no tool, because the cost of each report is a human reading a query
and a schema. So the bar is not "the two results differ" but "the two
results differ AND the relation between them is one SQL guarantees".

That rules out, by construction rather than by filtering afterwards:

- Anything whose row order is unspecified. Every comparison is between
  sorted multisets, never between result sequences.
- Floating point. Generated data is integers, text and NULL only.
- Any predicate whose value depends on collation, locale or the current
  time.

The second half of the invariant is the one with teeth: **a run that finds
nothing has to be distinguishable from a run that tested nothing.** See the
oracle below.

## Oracle

Ranked by how little of it we control.

1. **The metamorphic relation itself.** This is the whole point and it is
   unusually strong: it needs no second implementation, no stored expected
   output, and no model of what the query means.

   TLP (Rigger and Su, OOPSLA 2020). SQL is three-valued, so for any
   predicate `p` every row makes `p` exactly one of TRUE, FALSE or NULL,
   and the three partitions must reassemble into the whole:

       SELECT * FROM t WHERE p
         UNION ALL SELECT * FROM t WHERE NOT p
         UNION ALL SELECT * FROM t WHERE p IS NULL
       ==  SELECT * FROM t

   NoREC (Rigger and Su, ESEC/FSE 2020). The same query written so the
   optimiser cannot use an index must return the same count as the form
   where it can:

       SELECT COUNT(*) FROM t WHERE p
       ==  SELECT SUM(p IS TRUE) FROM t

   Neither can be satisfied by being self-consistently wrong in the way a
   hand-written expectation can.

2. **The SQL standard's three-valued truth tables**, transcribed in
   `tlp.py` and checked exhaustively against SQLite. This is a
   CALIBRATION, not the oracle, and the distinction matters: the tables are
   written down by hand here, which is exactly the thing this project
   otherwise avoids. They exist to prove the model of three-valued logic
   the generator is built on matches the engine, before anything is built
   on it. 34,353 expressions at depth 2.

3. **The generator's own vacuity measurement.** A predicate that is never
   NULL leaves TLP's third arm empty and reduces the test to "SQLite agrees
   with itself about TRUE and FALSE", which it will, forever, while
   printing a large and encouraging number. The fraction of generated
   predicates that actually produce a NULL is therefore measured and
   reported, not assumed.

## Non-goals

Crash finding. Fuzzers that look for segfaults in SQLite already exist and
are better at it; this is for wrong answers, which they cannot see.

Other engines. There is no DuckDB or PostgreSQL on this machine, and a
metamorphic oracle does not need one -- which is the reason for choosing
this technique over differential testing rather than a limitation of it.

Finding a bug in SQLite. SQLite is among the most heavily tested software
in existence and the published results for these techniques found most of
what was there. A run that reports nothing is the expected outcome; what
this has to prove is that it WOULD report something, which is what the
planted-defect checks are for.

Schema features beyond what the generator emits: no triggers, no views, no
foreign keys, no virtual tables, no window functions.

## Commit sequence

- [x] 00 three-valued logic, exhaustively, and the check broken on purpose
      why: everything else rests on SQL's three values behaving the way the
           generator assumes. Calibrate first, then build.
      files: tlp.py t/test_tlp.py Makefile
      proves: 34,353 expressions at depth 2 agree; four wrong truth tables
              are each caught; one rewrite is recorded as an equivalent
              mutant rather than chased.
- [x] 01 schema and data generation, with NULLs by construction
      why: a table with no NULLs cannot exercise three-valued logic at all,
           so the NULL rate is a parameter rather than an accident.
- [x] 02 the predicate generator, and `tlp vacuity`
      why: the generator decides whether any of this means anything, and
           the measurement has to exist before the fuzzer that depends on
           it. Expect the first version to be mostly two-valued.
- [x] 03 the TLP oracle
- [x] 04 the NoREC oracle
      why: TLP partitions the rows; NoREC attacks the optimiser directly.
           They fail on different bugs and neither subsumes the other.
- [x] 05 planted bugs: a wrapper that corrupts results on purpose, so the
      oracles can be watched to fail
      why: this is the only way to know the tool works, since the thing it
           is pointed at is almost certainly correct.
      done: four bug classes, and the table is the justification for
            carrying two oracles rather than one:
              drop-row           TLP 100%,  NoREC 0%
              duplicate-row      TLP 100%,  NoREC 0%
              null-only          TLP  23%,  NoREC 0%
              count-off-by-one   TLP   0%,  NoREC 100%
            TLP never asks for a count, so it cannot see one that is
            wrong; NoREC only ever compares two numbers, so it cannot see
            which rows moved. Neither subsumes the other, measured rather
            than asserted.
      found: the narrow bug was not narrow. "null-only" was meant to fire
            only for predicates mentioning NULL and was written as
            `"IS NULL" in sql` -- but the partition query ends in
            `(p) IS NULL` whatever p is, so it fired every time and was
            caught 100% of the time, exactly like the broad ones. Counting
            occurrences instead takes it to 23%, which is the honest shape
            for a narrow bug met by a random generator.
      note: the NULL rate has an optimum, which was not obvious until it
            was measured. At 0 nothing is three-valued; at 0.6 the rows
            are so NULL-heavy that the TRUE and FALSE arms empty out
            instead and 13% of predicates become constant. 0.25 is the
            peak at 29%, and the sweep is in the README. (It read 32% until
            item 12 put pattern matching into the shared generator.)
- [x] 06 `tlp reduce` -- shrink a failing case to a minimal schema,
      dataset and predicate
      why: an unreduced report is a thousand rows and a predicate nobody
           will read, which is the same as no report.
      done: greedy delta debugging over three axes -- drop a row, replace a
            predicate node with one of its own subtrees, pull a value
            toward NULL. Needed the generator refactored to hand back a
            TREE rather than a finished string; `gen_predicate` is now
            `render(gen_tree(...))` so every existing oracle is unchanged,
            which was checked by the suite staying green at the same
            numbers across the refactor.
      proves: SOUNDNESS, which is the only property that matters -- what
            comes out must still fail. A reducer that shrinks past the bug
            emits a minimal case reproducing nothing and sends its reader
            hunting for a bug that is not there, which is worse than
            printing the unreduced case. Asserted on every reduction the
            suite performs.
            And because the bugs are planted their true floors are known,
            so effectiveness is a measurement rather than a smoke test:
              drop-row       fires above 3 rows  -> reduces to exactly 4
              duplicate-row  fires on any rows   -> reduces to exactly 1
              null-only      fires above 2 rows  -> reduces to exactly 3
            "Smaller than before" would be satisfied by a reducer that
            drops one row and gives up.
            The null-only case is the one worth looking at: that bug also
            requires the predicate to mention NULL, the reducer was told
            neither condition, and it landed on 3 rows and `c0 IS NULL`.
            It rediscovered the trigger by shrinking.
      found: the shrink order was not well-founded and the reducer did not
            terminate. The value axis offered both NULL and 0 to every
            value and counted any accepted swap as progress, so NULL became
            0 and 0 became NULL forever, each step locally an improvement.
            Candidates are now strictly below the current value on a total
            order and the suite asserts no candidate leads back. Removing
            the strictness makes the suite HANG rather than fail, which is
            how that mutation honestly shows up.
      found: the reduced-case printer emitted Python, not SQL. Rows came
            out as `INSERT INTO t VALUES (None, None);` -- and the entire
            value of a reduced case is that it can be pasted into sqlite3.
            The suite now re-executes the printed INSERTs and compares the
            multiset it gets back.
      found: and that round-trip test was itself broken, exposed by a
            mutation rather than by a passing run. It compared with
            `sorted()`, which raises on a row mixing NULL with an integer;
            every case that happened to be green was all-NULL, so it never
            fired. Comparing `Counter`s is both correct and the right
            relation, since these are multisets.
      survived: breaking soundness -- dropping a row without re-checking --
            is caught for drop-row and null-only but NOT for
            duplicate-row, which is an equivalent mutant: that bug fires on
            any non-empty result, so dropping rows unconditionally lands on
            its real floor of 1 and the case still fails. Recorded rather
            than chased.

### Later

- [x] 07 partition on a JOIN's ON clause rather than a WHERE, which is
      where the published work found the most bugs
      done: two relations, both derived and checked by hand on a worked
            4x3 example before anything was built on them.
            INNER: the three ON-partitions are a partition of the cross
            product, because every pair makes the predicate exactly one of
            the three values and an inner join keeps the TRUE ones.
            LEFT: the same partitioning does NOT hold -- each arm pads its
            own unmatched rows and the padding is counted three times. The
            decomposition does: a left join is its inner join plus one row
            per left row with no TRUE match. That brings NOT EXISTS into
            the picture, which is a correlated subquery the optimiser
            rewrites into something else entirely.
            Every generated ON predicate is forced to mention both tables.
            Without it the predicate is a filter the planner pushes down
            and the join is not the join being tested. 65% genuinely split
            the cross product.
      proves: join-drop-pair  INNER 100%, LEFT   0%
              leftjoin-overpad INNER  0%, LEFT 100%
            Kept as its own table rather than folded into the single-table
            one, because those oracles query t and these query t1 and t2 --
            "TLP does not catch this" would only mean they were never
            pointed at the same thing.
      found: a hand-written expectation was wrong, again. The worked
            example asserts the join keeps 3 pairs; the first version said
            2. SQLite was right. The case stays, with the three TRUE pairs
            enumerated in the comment, because a relation that has only
            ever met generated data is a relation nobody has checked -- if
            it were wrong the generator would produce thousands of cases
            agreeing with it.
- [x] 08 aggregate TLP: SUM, COUNT, MIN and MAX over the three partitions
      must reassemble, which needs a different composition rule per
      aggregate and is where this gets interesting
      done: SUM recombines with SUM, MIN with MIN, MAX with MAX -- and
            COUNT with SUM, because counts add rather than counting. AVG
            is left out rather than approximated: the average of three
            averages is not the average, and weighting them needs the
            counts, which is a different query.
      proves: the rules are load-bearing, which matters because they look
            arbitrary. Against a correct database each right rule raises
            0 alarms in 210 checks and each wrong one raises 125 to 210.
            COUNT is the one to stare at: writing COUNT as its own
            combiner, which is the obvious mistake, reports a bug in 208
            of 210 correct queries.
            And it catches what the other two cannot: an aggregate wrong
            while every row is present and every count agrees is caught
            85% of the time by this oracle and 0% by either other.
      found: the planted bug for it was hitting the wrong query. It fired
            on anything starting "SELECT SUM(", which includes NoREC's own
            unoptimised form, so the table showed NoREC catching it 100%
            of the time -- what NoREC had actually noticed was the test
            wrapper breaking NoREC. Excluding subqueries gives the honest
            row: 0%, 0%, 85%.
- [x] 09 DISTINCT and GROUP BY partitioning
      why: each is the partitioning done over a query shape where the
           obvious recombination is the wrong one, and the two are wrong in
           OPPOSITE directions, which is the most instructive version of
           the trap the aggregate rules set.
      done: DISTINCT recombines with UNION -- a set union, and the only
            relation here that is not UNION ALL -- because a value can
            appear in two partitions: two rows sharing it can land on
            opposite sides of the predicate.
            GROUP BY must recombine with UNION ALL inside, for exactly the
            reason DISTINCT must not: each arm holds part of every group
            and the three partial counts are the point, so a set union
            would collapse two arms that counted the same number of rows.
            The outer step is then COUNT-recombines-with-SUM, group by
            group.
      proves: each rule is a false-alarm generator in the other's place,
            against a correct database, in 200 checks:
              DISTINCT   UNION 0,  UNION ALL 113, INTERSECT 200, EXCEPT 198
              GROUP BY   SUM   0,  COUNT 196,     MIN 113,       MAX/AVG 113
            And each relation catches exactly its own class: a dedup
            skipped on one path is caught 100% by DISTINCT and 0% by the
            four others; one group counted wrong is caught 100% by GROUP BY
            and 0% by the four others. Both leave every row present, every
            count right and every whole-table aggregate right.
      note: the 113 is one number appearing four times, and it is one
            event seen from two sides -- a value landing in more than one
            partition. UNION ALL then keeps it twice; the per-arm counts
            then sum above any single arm. Measured (113 of 200 span a
            partition) rather than left as a coincidence.
      note: the DISTINCT relation rests on DISTINCT, UNION and GROUP BY all
            treating two NULLs as the same value while `=` does not. If
            UNION followed the `=` rule a NULL from two arms would survive
            twice and the relation would be false for every table holding
            one. All three operators are checked directly.
      found: a zero in the selftest table that did not mean what it looked
            like. DISTINCT reads 0% against drop-row, which reads as "it
            cannot see a lost row" -- but drop-row is written to fire on
            "UNION ALL" and this relation recombines with "UNION", so the
            0% is a property of the trigger string. Added setop-drop-row,
            the same bug on any set operation: the column moves to 80%, so
            the relation sees lost rows and it was the bug that could not
            see the relation. That row of the table exists only to answer
            that question, and without it the table overclaims.
      found: and a vacuous test of my own, caught by mutation rather than
            by a run. Both false-alarm checks passed the composition rule
            in explicitly -- false_alarms(distinct_check, combine="UNION")
            -- so they tested a configuration no caller uses and left the
            shipped default unchecked. Changing the default to UNION ALL
            kept both green. A parameterised test that always supplies the
            parameter has stopped testing the thing that ships; they now
            call with no keyword and assert the defaults separately.
      found: run_oracle and cmd_agg printed found cases as Python reprs
            rather than SQL, the same defect the reducer had. Both now use
            sql_literal.

- [x] 10 HAVING partitioning
      why: HAVING is where the grouped equivalent of a pushed-down
           predicate goes wrong, and item 09 deliberately left it out.
           The relation is not the obvious one: a HAVING clause filters
           GROUPS, so partitioning it does not partition rows at all.
      done: and the recombination turned out to be the SIMPLEST here, in
            the exact place item 09 needed the most care. Plain UNION ALL,
            no re-aggregation: the groups are formed by the GROUP BY before
            HAVING runs, so each one arrives whole in exactly one arm,
            already counted. Item 09 needed SUM(n) because partitioning a
            WHERE cuts every group into three pieces; partitioning a
            HAVING cuts no group at all.
      proves: what is load-bearing MOVES. For DISTINCT and GROUP BY the
            composition operator was the trap; here it is not a trap and
            the traps are all in the arms, in 200 checks:
              as shipped                        0
              UNION instead of UNION ALL        0   <- equivalent rewrite
              drop the IS NULL arm             88
              IS NOT TRUE beside an IS NULL arm 88
              a duplicated TRUE arm           136
            UNION costs nothing because each arm emits at most one row per
            group key and the key is unique within a group, so the
            recombined query never holds two identical rows. The one place
            in this project where the obvious-looking mistake is not one,
            and recorded rather than quietly relied on.
      proves: having-drop-group is caught 98% by this relation and 0% by
            the five others. Nothing else writes a HAVING clause.
      found: THE THREE-WAY SPLIT IS A CHOICE, NOT A CORRECTNESS
            REQUIREMENT, which changed what I thought this project was
            doing. `h IS NOT TRUE` is exactly FALSE-or-NULL, so two arms
            already cover every group once, and the two-arm split raises 0
            false alarms in 200. It is not a quirk of HAVING either: the
            same two-arm split holds for the row-level relation this
            project is NAMED after, checked directly over 200 queries.
            "Ternary" describes how the technique is usually written, not
            something it needs to be valid.
      found: and the obvious follow-up claim -- that three arms is at
            least the STRONGER test, since it issues a third query shape --
            is not supported by anything measured here. Three arms and two
            arms detect drop-row, duplicate-row, null-only and
            setop-drop-row at identical rates.
            That is a finding about the PLANTED BUGS, not the relations.
            They corrupt result sets rather than query plans, so they
            cannot tell two sound relations apart however differently a
            real engine would execute them. It is the ceiling of this whole
            selftest approach and now says so in the README, because the
            planted-bug table is the first thing in that file and would
            otherwise read as measuring more than it does.
      found: the vacuity is structurally capped, not merely low. The
            generator reaches 18% three-valued against the row level's 29%,
            and an eight-row table has about 3.9 groups -- splitting four
            things three ways without emptying an arm is harder than
            splitting eight. Measured rather than excused.
            The aggregate mix is the lever: SUM, MIN and MAX go NULL on an
            all-NULL group while the COUNTs never do, so a COUNT-only
            generator leaves the third arm permanently empty (0%). 10%
            COUNT terms at NULL rate 0.40 is the measured optimum, and the
            sweep has the same interior peak as the row-level NULL rate for
            the same reason. `having` is the one command whose NULL-rate
            default is not 0.25.
      found: a self-comparing test of mine, caught by mutation. The check
            that the shipped arms are the correct ones compared
            having_check.__defaults__ against tlp.HAVING_ARMS -- the
            constant under test -- so it passed for any value that constant
            held. Cutting the shipped arms to a two-way split left it
            green. The literal tuple is now written out.
- [x] 11 a third table in the join, so reordering is actually exercised
      why: item 07 joins exactly two tables, which gives the planner
           almost no freedom to reorder -- and reordering is where it has
           the most freedom to be wrong. The README said so plainly and
           that admission is why this was next rather than later.
      done: two relations. The ON-partitioning carries over with the left
            side of the partitioned join now a join itself. And a new one
            that could not exist with two tables: SQLite documents CROSS
            JOIN as suppressing its own reordering, so the same query
            written both ways must agree, which differences the planner's
            chosen order against a fixed one.
      proves: the two forms reach genuinely different machinery -- the free
            form was observed visiting t1, t3, t2 and building an automatic
            covering index and a Bloom filter where the forced form scanned
            t3 last and did neither.
            reorder-skew is caught 30% by the reordering relation and 0% by
            the three others; a lost pair is caught 48% by the three-table
            ON-partitioning and 0% by the reordering one.
            And because the whole relation rests on CROSS JOIN pinning the
            order, the suite asks EXPLAIN QUERY PLAN whether it does rather
            than taking it from the documentation.
      note: the relation is worth nothing in the runs where SQLite picks
            the same order both ways, so the fraction where it actually
            reordered is measured and reported like the three-valued
            fraction. 62% on the default run.
      found: A LEVER THAT DID NOT SURVIVE BEING MEASURED PROPERLY, and the
            test asserting it passed. A minimal probe -- two cross-table
            comparisons and nothing else -- said equality predicates and an
            index moved the reordering rate from 15% to 67%, so the
            generator was built around both. On the shipped generator
            equality is worth 1 to 6 points and the index 6 to 11, with 15
            to 19 for both, measured over four seeds. The direction holds
            everywhere; the size did not transfer, because the real
            predicates already carry several cross-table comparisons and
            the planner has reasons to reorder without the help.
            The assertion written for the equality lever read NOISE: 58%
            against 64% at 200 cases is 61% against 60% at 500. What is
            asserted now is the combined effect, whose margin is 15 to 19
            points, and the individual rates are printed without an
            assertion the sample cannot support.
      note: reorder-skew is openly a PROXY. SQLite cannot be made to choose
            a wrong order on request, so what is corrupted is the CROSS
            JOIN form, whose order is pinned. The effect on the relation
            matches a planner that got the free order wrong; the limitation
            is that a real reordering bug would also change the plan, which
            this cannot. Recorded in the README rather than left implicit.
- [x] 12 LIKE and GLOB, with the case folding pinned explicitly
      why: a rich source of behaviour, deferred because it drags in
           collation, which the hard invariant excludes. Pinning the
           folding is what keeps it inside the invariant.
      done: and it turned out to be the one DIFFERENTIAL oracle here
            rather than a metamorphic one, still with no second database:
            SQLite ships two independent pattern matchers. Over a
            restricted alphabet LIKE and GLOB say the same thing under
            % -> * and _ -> ?, so the two implementations can be pointed
            at each other.
            Pattern matching went into the SHARED atom generator rather
            than only into its own relation, so it now appears inside the
            predicates every other oracle partitions -- about 1 in 4.
      proves: glob-skew is caught 27% by this relation and 0% by the six
            others. 852 value/pattern pairs agree, NULL included.
      note: two preconditions, both enforced rather than hoped for.
            LIKE folds ASCII case and GLOB never does, so
            case_sensitive_like=ON is pinned in a single connect() that
            every connection goes through -- a connection that forgot it
            would not fail loudly, it would measure something else. The
            suite checks that a connection WITHOUT the pragma answers
            differently, which is the only way to know the pinning does
            anything.
            And the rewrite is valid only over an alphabet free of GLOB
            metacharacters: 'axb' LIKE 'a*b' is FALSE while
            'axb' GLOB 'a*b' is TRUE, and the same for ? and for [ which
            opens a character class in one language and is a literal in
            the other. like_to_glob asserts the alphabet rather than
            trusting it, and the suite checks both that the assertion
            fires and that translating such a pattern anyway really would
            change what is matched.
      found: the README-consistency check from the previous commit earned
            its place immediately. Adding patterns to the shared generator
            moved every measured number in the file, and the suite went red
            on the composition tables rather than letting the document
            drift. Every table has been re-measured.

- [ ] 13 a view, and a CTE, expressing the same query
      why: every relation here compares two queries over base tables. A
           view is the same query wearing a name, and SQLite either flattens
           it into the outer query or materialises it -- two different plans
           for text that must mean the same thing. It is the cheapest way
           to get a second plan for one relation without a second engine,
           and it is the shape the reordering relation had to fake with
           CROSS JOIN.
- [ ] 14 HAVING over SUM, MIN and MAX, and a second grouping column
      why: item 10 partitions HAVING only for COUNT(*) on one grouping
           column, which is the easiest case: COUNT(*) is never NULL and
           one column groups NULLs into exactly one group. Two columns
           make the grouping key a tuple with its own NULL rule, and the
           other aggregates reach the composition table again.
- [ ] 15 LIKE with ESCAPE, and GLOB character classes
      why: item 12 bought LIKE at the price of a five-character pattern
           alphabet, because the LIKE-to-GLOB rewrite breaks on any GLOB
           metacharacter. ESCAPE and [...] are exactly the parts that were
           excluded, and they need an oracle that is not the other matcher
           -- most likely Python's fnmatch and a hand-written escape
           walker, which is a different kind of oracle from anything here.
- [ ] 16 the reducer shrinks the schema too
      why: an unused column survives into the reduced CREATE TABLE even
           when nothing in the surviving predicate mentions it, which the
           README shows in its own worked example. Recorded as an open
           question by item 06; the reason it was left out is that dropping
           a column changes the DDL rather than the data and every oracle
           here assumes a fixed schema for the life of a case, so this item
           is really about whether that assumption has to hold.

## Found after the sequence above

Not a planned item -- it came out of reading the selftest table rather than
the plan, and it is recorded here because it changed what the table means.

**The GROUP BY column was dominated by the HAVING column**, the same
percentage or lower on all ten planted bugs. The prose argument for
carrying both was and is correct: GROUP BY partitions a WHERE clause
underneath a grouping and re-aggregates the per-arm counts, HAVING
partitions the HAVING and re-aggregates nothing, and neither subsumes the
other. But the TABLE said otherwise, because nothing modelled a defect in
the path GROUP BY alone covers. On the available evidence the relation was
redundant.

`groupfilter-skew` is that defect -- a per-partition grouped count that is
wrong, which is grouping gone wrong under a filter. GROUP BY catches it
100%, HAVING 0%.

And the claim is now checked as a matrix instead of argued pair by pair:
for every ordered pair of relations there must be some planted bug the
first catches and the second does not. Deleting groupfilter-skew turns that
check red with "GROUP is dominated by HAVING", which is how the hole was
found in the first place. A design claim that is only ever argued in prose
is a design claim nobody has tested.

## Open questions

- RESOLVED by item 12. Yes, with the case folding pinned rather than the
  collation: `case_sensitive_like=ON` in a single `connect()`. The pragma
  bought more than access to `LIKE` -- it made `GLOB` usable as `LIKE`'s
  reference implementation, which is the only differential oracle in this
  project.
- Whether the planted bugs can be made to model a wrong PLAN rather than a
  wrong RESULT. They wrap the connection and corrupt result sets, which is
  why the two-arm and three-arm splits score identically and why
  `reorder-skew` has to corrupt the pinned-order query as a proxy. No idea
  yet what the honest version looks like without patching SQLite.
- RESOLVED by item 06. Reduction does both, and the data axis matters
  more: rows go 10 -> 1 while predicates go 13 nodes -> 1. The two
  interact in the direction that helps, since a smaller predicate makes
  more rows droppable. The axis that caused trouble was neither -- it was
  shrinking VALUES, which needs a well-founded order or the loop runs
  forever.
- Whether the schema should shrink too. An unused column survives into the
  reduced CREATE TABLE even when nothing in the surviving predicate
  mentions it. Cheap to add and it would make reports shorter; left out
  because dropping a column changes the DDL rather than the data, and
  every oracle here assumes the schema is fixed for the life of a case.
