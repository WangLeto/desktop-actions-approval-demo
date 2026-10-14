# Synthetic desktop publication demo

Only tiny synthetic files are used. No real installers, signing, cloud storage,
production services, credentials, or remote release tags are involved.

1. Run **Package synthetic files**. There is no publish option.
2. Later run **Publish selected package**, entering its run ID and optionally its
   attempt. Blank selects the latest attempt, which must have succeeded; it never
   falls back to an older successful attempt.
3. Read the prepare job summary, then use **Review deployments** to approve or reject.
4. `interrupt_once` persists one fake object then fails on publishing attempt 1.
   Re-run failed jobs to verify the same approval digest, skip that object, upload
   missing objects, and finish the YAML feed.

The `demo-store` branch's `state.json` is the fake object store. Each object write
is persisted using GitHub Contents API compare-and-swap. A workflow concurrency
lock serializes publishers (more restrictive than a channel lock). This is not a
simulation of persistent self-hosted runner routing. GitHub concurrency is not a
FIFO queue and can replace pending runs. Production tag semantics are tested only
with an in-memory transport; no GitHub tag API is called.

Run local checks: `python3 -m unittest discover -s tests -v`.
