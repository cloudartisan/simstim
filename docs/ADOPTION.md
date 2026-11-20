# Adopt upstream SIMSTIM changes locally

Keep contribution, verification and deployment separate. Submit changes through a branch and pull request; review and required checks belong on protected `main`. A merge does not authorise deployment. The helper fetches `refs/heads/main` from `origin`; it does not verify hosting-side branch protection or CI. Check those separately before adoption.

## 1. Fetch and review without changing a worktree

Start with the **full commit SHA actually deployed** for the component you are updating, from your deployment record. Do not substitute `HEAD`, `main` or a guessed baseline. If you cannot establish the pin, stop.

Use a new, standalone verification clone outside the live source tree, with no live config, credentials or attached hardware. Use a trusted copy of `check-upstream`, not a copy taken from the candidate you are about to review. Example with Git 2.47.3 (the tested version):

```sh
# Set PIN to the recorded deployed SHA first.
git clone --no-local --no-checkout https://github.com/cloudartisan/simstim.git simstim-verify
cd simstim-verify
git remote get-url origin   # confirm this is your trusted upstream
git switch --detach "$PIN"
/path/to/trusted/scripts/check-upstream "$PIN"
```

For offline use, substitute a trusted local mirror or bare origin path for the URL. The mirror's `main` is only as current as its last approved sync. Use a normal full clone, not a linked worktree, shallow clone or shared object store. Run one check at a time and do not fetch or edit the verification clone concurrently.

The helper requires a clean detached checkout, including no untracked or ignored files. It fetches objects and writes `FETCH_HEAD` in that clone, but does not switch commits, update local or remote-tracking branches, or execute candidate code. It prints the exact `Candidate:` SHA, incoming commit subjects and net changed paths. `No incoming changes.` means there is nothing to adopt. On a non-zero exit, stop; do not reuse an older candidate. If the pin is missing or not an ancestor of fetched `main`, investigate before proceeding. Never reset or clean a checkout to make a check pass.

## 2. Test exactly what you reviewed

Set `CANDIDATE` to the full SHA from a **successful** check. Review the patch, confirm the verification checkout is still clean, then switch explicitly to the candidate. Do not follow a moving branch for the test.

```sh
git --no-pager diff --no-ext-diff --no-textconv "$PIN" "$CANDIDATE" --
git status --porcelain=v1 --untracked-files=all --ignored=matching
# Continue only if the status command succeeded and printed nothing.
git switch --detach --no-overwrite-ignore "$CANDIDATE"
git rev-parse HEAD   # confirm this is exactly CANDIDATE
```

Read candidate test and build instructions before executing them. Run the relevant offline tests and build-only checks in this disposable clone without live credentials or hardware. For example, for wireless host changes run `(cd wireless-control && python3 -B -m unittest discover -s tests -v)` after confirming the suite is offline. Record the exact SHA, commands and results. Offline checks do not prove hardware compatibility. If tests leave files behind, use a fresh verification clone for the next check instead of deleting unfamiliar files.

## 3. Decide on deployment separately

Successful verification is not deployment approval. Before a separate operator-approved rollout, record the tested SHA and rollback image or host build, and check that any existing deploy checkout is clean and its `HEAD` still equals the recorded deployed pin. If it is dirty, divergent or does not match the pin, stop and preserve local work; reconcile it separately, then re-test. Deploy only the tested revision through the authorised procedure and update the deployment record only after verification of the rollout. Do not `git pull` into live source. This workflow does not install services, restart anything or flash hardware.

To test the helper itself from the repository root, run `sh -n scripts/check-upstream` and `python3 -B tests/test_check_upstream.py -v`. The integration tests use disposable local Git repositories and fake commits only.
