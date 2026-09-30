# Agent guide

## Verification

Run the whole test suite before every commit, exactly as written:

    python3 -m unittest discover -s tests -v

## Rules

- Keep the package standard-library only.
- Document every public function in README.md under "Functions".
- Give every error path its own test.
