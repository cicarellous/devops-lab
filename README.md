# devops-lab

A Python command-line tool for log analysis and operational troubleshooting. It reads logs from files or standard input, counts entries by severity, and supports text filtering and line-numbered output for incident triage.

Run the log summary script:

```powershell
python log_summary.py
```

Run its unit tests:

```powershell
python -m unittest -v
```

GitHub Actions runs the unit tests on pushes to `main` and on pull requests targeting `main`. You can see each run in the repository's **Actions** tab.