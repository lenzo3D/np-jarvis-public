# Publishing to the public repo (maintainer notes)

The private repo is where work happens. Every push to `main` is copied to the public repo as one new commit by
`.github/workflows/sync-public.yml`, so friends who update from the public repo get each new version.
Private history is never copied. Files listed in `.publicignore` stay private.

## One-time setup
1. Create an empty public repo on GitHub (no README).
2. Publish the first copy: `scripts/export_clean.sh ../jarvis-public`, then in that folder
   `git remote add origin <public repo url> && git push -u origin main`.
3. Create a fine-grained personal access token (GitHub > Settings > Developer settings): only the public repo,
   permission **Contents: Read and write**.
4. In the private repo: Settings > Secrets and variables > Actions. Add the secret `PUBLIC_REPO_TOKEN` (the token)
   and the variable `PUBLIC_REPO` (`lenzo3D/np-jarvis-public`).
5. Push anything to `main` (or run the workflow by hand from the Actions tab) and check the public repo updated.

## Rules
- Never commit `secrets.env`, `state/` or real account data. The script refuses to publish if they are tracked.
- Friends' panels update from the public repo, so keep `main` working. CI runs the tests on every push.
