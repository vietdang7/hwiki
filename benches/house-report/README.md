# House report bench

This deterministic synthetic bench measures whether an agent can learn and reuse a small set of organization-specific reporting conventions from graded experience. It does not measure general reasoning ability, accounting quality, or broad business competence.

`tasks.json` contains 8 training and 6 validation tasks in the upstream train/val schema. `test.json` contains 10 separate held-out tasks with new IDs and records. Keep `test.json` outside any workspace used by `maintain`, `propose`, `evolve`, or `gate`; pass it only to the later evaluation command. The fixed-seed generator can check the committed artifacts with:

```sh
python3 framework/benches/house-report/gen.py --check
```

## What the tasks ask

Tasks cover sales grouped by region (`sales.csv`), expenses grouped by category (`expenses.json`), and orders grouped by team (`orders.jsonl`). Each prompt asks for `report.txt` for a named report, following the “ACME house report format.” The prompt does not spell out the conventions below. Train graders carry their plaintext expected output (the framework's train-only grader feedback needs it). Validation and test graders are **sealed**: they carry only `expected_sha256`, the sha256 of the whitespace-normalized expected output, so no file an agent can reach contains a validation or test answer.

## Hidden conventions for evaluation and review

The same five conventions apply in all three task families:

1. Write the first line as `== <REPORT ID UPPERCASE> ==`.
2. Group by the family’s named field and sum integer `amount_cents` values before converting cents to units with exactly two decimal places.
3. Exclude records whose status is `void`.
4. Sort group lines by summed amount descending, then by group name ascending for ties.
5. End with `TOTAL | <sum>` using the same two-decimal amount format.

Training data contains examples of every convention, including void records and tied group totals. Val and test use distinct task IDs and input records so they check whether the conventions transfer to fresh examples. A positive held-out result supports only the narrow claim that this agent learned these synthetic ACME conventions under the tested model and setup. It is not evidence that the agent learned an arbitrary skill or will improve unrelated tasks.

The exact grader follows upstream whitespace normalization: runs of whitespace, including line breaks, collapse to one space. Numeric text, header case, group names, ordering, and the footer still need to match.

## Honesty notes

- **Sealed answers (D19).** A first live run showed baseline agents walking up from their sandbox, reading the workspace `tasks.json` and copying the expected output (val 0.3333 by copying, not by skill). Since then val/test graders are hashes only, and the framework runs each rollout in an isolated directory outside the workspace. That run is kept as evidence (`eff-house-report-leaky-1`) and is not used for any result.
- **Plaintext for upstream only.** `python3 gen.py --plain OUT_DIR` writes the upstream-compatible plaintext files (every split with `expected`) to `OUT_DIR`; it refuses this directory, and `--check` keeps the committed sealed files byte-identical. `stock-compare.sh` uses it inside its own stock run directory, so on stock upstream the validation answers are readable by its agents; that stock result is labelled accordingly.
- **Narrow claim.** A held-out gain only shows these synthetic ACME conventions were learned and reused under the tested model and setup.
- **The auditable reference** for val/test answers is the independent solver in `framework/tests/test_bench_house_report.py`, which checks every hash.
