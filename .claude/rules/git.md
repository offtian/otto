---
paths:
  - "**/*"
---

# Git & PR Conventions

## Commit Messages
- Write subjects in imperative mood: "Add feature" not "Added feature" or "Adds feature"
- Format: `<emoji> <subject>` or just `<subject>`
- Each commit should do only ONE thing
- Don't mix refactoring with functional changes in the same commit
- Each commit should be atomic — the codebase should work after each commit

## PR Conventions
- Keep PRs small and focused
- Link to the Asana ticket in the PR description
- Chain PRs by editing the base branch when a PR depends on another
- Ensure reviewers have enough context in the description