---
name: amagi-env-check
description: Compare a project's .env, .env.example and its live deploy environment (Vercel, Railway or Fly) by key name. Reports keys that are missing, extra, empty or duplicated, and never prints a value. Use before a deploy, when a deploy broke on a missing or empty env var, when .env.example has drifted from what production actually has, or when asked which env vars need to be added to Vercel, Railway or Fly.
---

# amagi-env-check

A CLI that lines up three views of a project's environment variables and reports where they disagree:
the example file (`.env.example`), the local file (`.env`, else `.env.local`), and the live platform
environment. It works on key names only. Values are read just long enough to check for emptiness and
are never printed, logged or stored.

## When to use this skill

- The user is about to deploy and wants to know whether the platform has every key the app expects.
- A deploy failed or a feature broke in production because a variable was missing or empty.
- The user added keys locally and wants to know which ones still need to go into Vercel, Railway or Fly.
- `.env.example` may be stale: keys in production or `.env` that nobody documented.

## How to use it

Install once: `pipx install amagi-env-check` (or `pip install -e .` from this repo in development).

```bash
amagi-env-check                                    # .env vs .env.example in the current folder
amagi-env-check --run vercel                       # also the Vercel production env (needs a linked, logged-in vercel CLI)
amagi-env-check --run vercel --target preview      # another Vercel environment
amagi-env-check --run railway --run-args "--service api"
amagi-env-check --run fly --run-args "-a my-app"
vercel env ls production | amagi-env-check --live -   # piped or saved CLI output works too
amagi-env-check --format json --matrix             # machine-readable findings plus a key-by-source map
```

Exit code 0 means no problems, 1 means problems were found, 2 means a usage or input error
(including a platform CLI that failed; the error message shows its last lines).

## The four categories

- `missing`: the reference (the example file, else `.env`) lists the key and this source lacks it.
  Missing in the live source is the one that breaks deploys. Fix it in the platform dashboard or CLI.
- `extra`: the source has a key the example file never mentions. Add it to `.env.example` or remove it.
- `empty`: the key exists with an empty value. Keys commented out in the example (`# KEY=`) are
  optional and may be empty. Vercel and Fly hide values, so their keys show as `present`, never `empty`.
- `duplicate`: the key appears twice in one file; most loaders keep the last one.

## Working with the results

1. Run `amagi-env-check --run <platform> --format json` and read `findings`. Each item has `category`,
   `source`, `key` and `lines`. `sources[].values_known` tells you whether emptiness could be checked.
2. For keys missing in the live source, tell the user the exact names and the platform command to add
   them (for example `vercel env add NAME production`). Never ask for, print or copy a secret value.
3. For `extra` keys, ask whether they are still used before suggesting deletion. Platform-injected keys
   (`VERCEL_*`, `RAILWAY_*`, `FLY_*`, `TURBO_*`) are already skipped unless the example lists them.
4. Silence known differences with `--ignore 'GLOB'` (repeatable) or a `.envcheckignore` file, one name or
   glob per line.
5. Re-run until it exits 0, or use `--fail-on missing` if only missing keys should block a deploy.
