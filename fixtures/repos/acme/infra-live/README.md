# infra-live (fixture)

A stand-in for a team-owned infrastructure repository: Otto's dev host
(`LOCAL_REPOS_DIR`) reads it, and the tests and evals propose changes to it.
Nothing here is applied anywhere.

## Conventions

- Workspace quotas live in `coder/quotas.tfvars`, one line per team, keys in
  alphabetical order.
- Units are CPU cores, GiB of memory and GiB of disk.
- A quota change needs the requesting team's lead named in the pull request.
