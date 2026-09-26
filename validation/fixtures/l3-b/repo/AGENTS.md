# Agent guide

## Verification

The only verification command is the check script. Run it before every
commit, exactly as written:

    sh ./check.sh

## Rules

- Keep the package standard-library only.
- Document every public function in README.md under "Functions".
- Give every error path its own test.
