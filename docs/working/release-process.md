# Release Process

Use this document when you publish a new pr-pipeline version to installed
users. The goal is a release that existing installs receive, that keeps their
configuration intact, and that has a changelog entry to explain it.

## How an update reaches users

The repository is its own plugin marketplace. There is no registry, upload, or
build step. The normal sequence is:

1. A release PR changes `version` in `.claude-plugin/plugin.json` and adds the
   matching `CHANGELOG.md` section. The owner merges it to `main`.
2. A user refreshes the catalog and updates the plugin:
   ```
   /plugin marketplace update pr-pipeline
   /plugin update pr-pipeline
   ```
3. The user restarts Claude Code. Claude Code installs the new version into a
   new cache directory. A session that was already running keeps the old
   `CLAUDE_PLUGIN_ROOT` and runs the old code until it restarts (see
   [DEBUG.md](../../skills/tick/DEBUG.md#the-install-path-resolves-in-two-different-places)).

The version in `plugin.json` is the update key. If a change merges to `main`
without a version change, existing installs do not receive it, although new
installs do. The marketplace entry must not declare a second `version`.
`test_plugin_manifest_is_the_only_version_field` enforces this rule.

The update keeps the user's state:

| State | Location | After an update |
|---|---|---|
| Operator configuration | `~/.claude/pr-pipeline/config.toml` | Unchanged; it is outside the install |
| Launcher | `~/.claude/pr-pipeline/bin/pr-pipeline` | Resolves the new install on its next call |
| Running `tick --loop` | Process memory | Runs the old code until restarted |
| Labels and dispatch markers | Target repositories | Unchanged |

## Choosing the version

Versions follow [Semantic Versioning](https://semver.org/):

| Change | Before 1.0.0 | From 1.0.0 |
|---|---|---|
| Fix with no contract change | Patch | Patch |
| New command, option, or configuration key | Minor | Minor |
| Change that requires a user step: configuration format, label vocabulary, launcher, or rerunning setup | Minor | Major |

## Release steps

1. Fetch the base and list the changes since the last release:
   ```bash
   git fetch origin --tags
   git log --format='%s' "$(git describe --tags --abbrev=0 origin/main)"..origin/main
   ```
   If no tag exists yet, list the full history of `origin/main`.
2. Create a release branch from `origin/main`.
3. Set `version` in `.claude-plugin/plugin.json`.
4. Add a `## [X.Y.Z] - YYYY-MM-DD` section to the top of `CHANGELOG.md`. Use the
   planned merge date. Group the entries under `Added`, `Changed`, `Fixed`, or
   `Removed`. Always write an `Upgrade steps` subsection, even if it says
   "None". Name every step that users must do, for example rerunning
   `/pr-pipeline:setup` or `pr-pipeline labels provision`.
5. Run the checks:
   ```bash
   python3.13 -m pytest skills/ -q
   claude plugin validate .claude-plugin/marketplace.json
   claude plugin validate .claude-plugin/plugin.json
   ```
   `test_changelog_records_the_manifest_version` fails if step 4 is missing.
   The plugin validation reports one expected warning about the root
   `CLAUDE.md`.
6. Open the release PR with the subject `chore: release X.Y.Z`. Use the
   changelog section as the summary.
7. After the owner merges, [tag the merge commit](#tagging-the-release).
8. Run the [upgrade smoke test](#upgrade-smoke-test) against the new tag and
   record the result in a comment on the release PR.

## Tagging the release

Tag the commit that merged the release PR. Do not tag the tip of `origin/main`:
another PR can merge after the release PR, and the tag would then include a
change that the release PR and its changelog do not describe. The next
release's `git log <last tag>..origin/main` would also omit that change.

The commands below get the merge commit from the PR, check that it is on
`main` and holds the release version and changelog section, and only then tag
and push it. If a check fails, nothing is tagged.

```bash
PR=<release PR number>; VERSION=X.Y.Z
git fetch origin --tags
SHA="$(gh pr view "$PR" --json state,mergeCommit -q 'select(.state == "MERGED") | .mergeCommit.oid')"
test -n "$SHA" \
  && git merge-base --is-ancestor "$SHA" origin/main \
  && test "$(git show "$SHA:.claude-plugin/plugin.json" | jq -r .version)" = "$VERSION" \
  && git show "$SHA:CHANGELOG.md" | grep -q "^## \[$VERSION\]" \
  && git tag -a "v$VERSION" "$SHA" -m "pr-pipeline $VERSION" \
  && git push origin "v$VERSION"
```

Optionally, publish a GitHub release from the changelog section:
`gh release create "v$VERSION" --verify-tag --notes-file <file>`. Claude Code
does not read tags or releases. They give users a changelog anchor and give
maintainers a comparison base for the next release.

## Upgrade smoke test

This test proves that an existing install receives the release and runs it
through the normal surfaces. A successful `marketplace update` alone does not
prove this. The
[L4 procedure](plugin-distribution-design.md#l4--upgrade-survival) explains
why. The test has two modes:

| Mode | When | What it proves |
|---|---|---|
| Upgrade | A previous release exists | The previous release updates to the new version and keeps its configuration |
| First install | Only for 0.1.0 | The new version installs and runs. It does not prove an upgrade |

Run the test after you [tag the release](#tagging-the-release). The test
installs from a local clone of this repository, checked out at release tags.
Do not install from `main` instead. A change can merge to `main` without a
version change. A fresh install from `main` gets that change under the previous
version number, but an existing install does not have it. A check that starts
from `main` can therefore pass while the previous release fails. With the tags,
the test starts from exactly the previous release and ends at exactly the new
one. The tag is also the oldest code that carries the previous version number:
users who installed after it run a later commit under the same number. The test
does not cover how `marketplace update` fetches from GitHub.

The test uses a temporary `CLAUDE_CONFIG_DIR`, so your own Claude Code
installation and operator configuration do not change. It does not run
`install-launcher.sh`: that script writes to `$HOME/.claude` even when
`CLAUDE_CONFIG_DIR` is set. The test copies the launcher into the temporary
directory instead.

The test also unsets `PR_PIPELINE_HOME` and `PR_PIPELINE_CONFIG`. When
`PR_PIPELINE_HOME` is set, the launcher runs that checkout and does not read
the plugin inventory. When `PR_PIPELINE_CONFIG` is set, it selects the operator
configuration instead of `$CLAUDE_CONFIG_DIR/pr-pipeline/config.toml`. With
either variable set, a check can pass against code or configuration that the
test did not install. Run all of the commands in one shell. Check 0 fails if
the shell sets either variable again.

You need:

- A target repository that your operator configuration already contains (run
  `/pr-pipeline:setup` once), and a local checkout of that repository.
- The path of the operator configuration that you use. This is
  `$PR_PIPELINE_CONFIG` if you set it. Otherwise it is
  `pr-pipeline/config.toml` in your `CLAUDE_CONFIG_DIR`, or in `~/.claude` if
  you do not set `CLAUDE_CONFIG_DIR`. Find the path before you run the commands
  below, because they change these variables.

### 1. Install the previous release

Set `PREVIOUS` to the previous release version. The command
`git describe --tags --abbrev=0 vX.Y.Z^` prints its tag. In the first-install
mode, set `PREVIOUS` to the new version.

```bash
VERSION=X.Y.Z; PREVIOUS=<previous release version>
TARGET=<owner/name>; TARGET_CHECKOUT=<absolute path to its checkout>
SOURCE_CONFIG=<absolute path to your operator configuration>
SMOKE="$(mktemp -d)"
export CLAUDE_CONFIG_DIR="$SMOKE/claude"
unset PR_PIPELINE_HOME PR_PIPELINE_CONFIG
mkdir -p "$CLAUDE_CONFIG_DIR/pr-pipeline/bin"
git clone --quiet https://github.com/kwuBuild/pr-pipeline.git "$SMOKE/marketplace"
git -C "$SMOKE/marketplace" checkout --quiet "v$PREVIOUS"
claude plugin marketplace add "$SMOKE/marketplace"
claude plugin install pr-pipeline@pr-pipeline
OLD="$(claude plugin list --json | jq -r '.[] | select(.id == "pr-pipeline@pr-pipeline") | .installPath')"
claude plugin list --json | jq -e --arg v "$PREVIOUS" '.[] | select(.id == "pr-pipeline@pr-pipeline") | .version == $v'
diff -rq -x .git "$SMOKE/marketplace" "$OLD"
cp "$SOURCE_CONFIG" "$CLAUDE_CONFIG_DIR/pr-pipeline/config.toml"
install -m 755 "$OLD/skills/tick/pr-pipeline.sh" "$CLAUDE_CONFIG_DIR/pr-pipeline/bin/pr-pipeline"
(cd "$CLAUDE_CONFIG_DIR" && shasum -a 256 pr-pipeline/config.toml > config.sha256)
```

The `jq` and `diff` commands must succeed. They prove that the install is the
previous version and holds exactly the code of its tag. The copied launcher
comes from that install. This matches a user's machine after an update: the
launcher copy stays at the old version and must find the new install.

### 2. Update to the new release and check

In the same shell, update the install. Skip these commands in the
first-install mode.

```bash
git -C "$SMOKE/marketplace" checkout --quiet "v$VERSION"
claude plugin marketplace update pr-pipeline
claude plugin update pr-pipeline@pr-pipeline
```

Then run the checks. Each one must succeed:

```bash
# 0. No override replaces the install or the configuration under test.
test -z "${PR_PIPELINE_HOME+x}${PR_PIPELINE_CONFIG+x}" && test "$CLAUDE_CONFIG_DIR" = "$SMOKE/claude"

# 1. Exactly one enabled install, at the new version and path, with the code of the new tag.
claude plugin list --json | jq -e --arg v "$VERSION" '[.[] | select(.id == "pr-pipeline@pr-pipeline" and .enabled)] | length == 1 and .[0].version == $v and (.[0].installPath | endswith("/" + $v))'
NEW="$(claude plugin list --json | jq -r '.[] | select(.id == "pr-pipeline@pr-pipeline") | .installPath')"
diff -rq -x .git "$SMOKE/marketplace" "$NEW"

# 2. Claude loads the new version's skills.
claude plugin details pr-pipeline | grep -x "pr-pipeline $VERSION"
claude plugin details pr-pipeline | grep 'Skills ('

# 3. The copied launcher selects the install and runs the read-only dashboard.
(cd "$TARGET_CHECKOUT" && "$CLAUDE_CONFIG_DIR/pr-pipeline/bin/pr-pipeline" status)

# 4. The operator configuration is unchanged and the new code parses it.
(cd "$CLAUDE_CONFIG_DIR" && shasum -a 256 -c config.sha256)
bash "$NEW/skills/setup/setup.sh" inspect "$TARGET" | jq -e '.configured and (.config_errors | length == 0)'
```

Compare the skill list from check 2 with the `skills/*/SKILL.md` directories of
the release. The update leaves the previous version's cache directory in
place. Checks 0 and 1 tie checks 3 and 4 to the new code and the copied
configuration. With no override set, the launcher runs the single enabled
install, and check 1 proves that this install is the new tag. The `inspect`
command reads `$CLAUDE_CONFIG_DIR/pr-pipeline/config.toml`, which is the file
that the checksum covers.

The checks fail in these known fault cases:

| Fault | Failing check |
|---|---|
| The install is disabled | 1 and 3 |
| A change after the previous tag breaks the launcher contract, for example a renamed entry script | 3 |
| The shell sets `PR_PIPELINE_HOME` or `PR_PIPELINE_CONFIG` again | 0 |
| The configuration changed | 4 |
| The configuration is not valid | 4 |

The test does not run a skill inside a Claude model session. That needs
credentials in the temporary directory and model calls. If the release changes
a skill body, run that skill in a new Claude Code session after you update your
own install, and record the result on the release PR.

Remove the temporary directory afterwards: `rm -rf "$SMOKE"`.

## When a release is broken

Fix forward. Revert or fix the change on `main`, then release a new patch
version. Do not move or delete a published tag, and do not reuse a version
number: installs that already received that version do not update again for
the same number.
