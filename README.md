# devops-lab

A small Python project for practicing scripting, testing, Git, and CI.

Run the log summary script:

```powershell
python log_summary.py
```

Run its unit tests:

```powershell
python -m unittest -v
```

GitHub Actions runs the unit tests on pushes to `main` and on pull requests targeting `main`. You can see each run in the repository's **Actions** tab.