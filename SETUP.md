# Pushing this to your public repo

## 1. Push the code

From the folder where you've downloaded these files:

```bash
git init -b main
git add .
git commit -m "Job scraper: 312 firms, verification, weekly digest"
git remote add origin https://github.com/<your-username>/job-scraper.git
git push -u origin main
```

If the repo already has a README, use `git pull --rebase origin main` before pushing.

No GitHub CLI needed. If you'd rather not use the terminal at all, GitHub's web
uploader takes a drag-and-drop of the whole folder — but it won't preserve the
`.github/` directory, so create `.github/workflows/weekly.yml` by hand after.

## 2. Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret.

| secret | needed for | where to get it |
|---|---|---|
| `DB_PASSPHRASE` | **required** — encrypts your job history | make one up, save it in your password manager |
| `REED_API_KEY` | Reed listings | reed.co.uk/developers, free |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | board coverage | developer.adzuna.com, free |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | digest to your phone | @BotFather |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` / `DIGEST_TO` | digest by email | Gmail app password |

Secrets are encrypted and are not readable from a public repo. They're also not
passed to workflow runs triggered by forks, so a fork of your repo can't use them.

## 3. First run

Actions tab → "weekly job run" → Run workflow. Don't wait for Sunday — you want
to see `sniff.py`'s hit rate before trusting anything downstream.

Then check the artifact on the run page for `report.html`.

## Because this repo is public

Three things are deliberately different from the private setup:

**Your job history is encrypted, not committed in the clear.** `jobs.db` holds
which firms you're tracking, every role you've seen, and what you've marked as
applied. On a public repo that's readable by anyone — including your current
employer and any firm you're applying to. It's committed as `jobs.db.gpg`
(AES-256) and decrypted only inside the run. Round-trip tested.

**Reports aren't committed.** `report.html` and `scored.csv` name the specific
roles you're pursuing. They're uploaded as a 90-day artifact instead, visible
only to you.

**`cv.txt` is gitignored.** Don't force-add it.

The upside of public: Actions minutes are unlimited, so the run costs nothing
regardless of how long it takes.

## What's still yours to decide

`firms.csv` will be public. That's 312 firm names and domains — no personal
information, and arguably useful to other people doing the same thing. But it
does signal what you're targeting, and it's the one file that makes the repo
obviously a job-search tool rather than a scraping library. If that bothers you,
either make the repo private or rename the project and keep `firms.csv` local
via `.gitignore`.
