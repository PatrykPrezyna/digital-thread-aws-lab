# Contributing

Thank you for helping improve this teaching lab. The repository is meant
to be forked by a course, used by students, and patched in the open.

## What kinds of changes are welcome

- Clarifications, typo fixes, and better checkpoints in the student guide
- Additional `RobotArmSource` implementations for real lab arms
  (ROS 2, OPC UA, Modbus, a particular serial protocol, …)
- Test coverage and IAM/policy tightening that still lets the 2-hour
  path work
- Translations of the student guide
- Alternative query engines or partition layouts, documented as
  *optional* so the default Athena path stays cheap

Please open an issue before large refactors (replacing Firehose, adding
Terraform as the only setup path, and so on) so instructors who already
scheduled the lab can comment.

## How to work

1. Fork, or branch off the default branch.
2. Keep changes in the spirit of the module: a shared event contract, a
   5-axis packing-arm device, least-privilege IAM, Athena by default, teardown
   and a budget alarm, and no cloud path presented as a safety function.
3. Run the local checks:

   ```bash
   python -m unittest discover -s tests -v
   bash -n infra/setup.sh infra/teardown.sh
   python -c "import json,pathlib; [json.loads(p.read_text()) for p in pathlib.Path('iam').glob('*.json')]"
   ```

4. Do not commit `infra/config.env`, AWS keys, device certificates, or
   student data.
5. Open a pull request that says which lab (if any) you touched and how
   you tested.

## Student work

If you are completing this as a class assignment, do **not** open a
pull request with your solutions, account IDs, or raw lab data. Hand
that work in through your course’s submission path. Fixes to the lab
itself (a broken flag, a wrong query, a misleading sentence) are
welcome and appreciated.

## Code of conduct

Participation is governed by `CODE_OF_CONDUCT.md`.
