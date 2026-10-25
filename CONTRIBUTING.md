# Contributing

Use Python 3.10+ and run `python tools/check.py` plus `python tools/check_release.py` before proposing changes. Keep inherited source bytes separate from new wrappers. If core code changes, add targeted CPU tests and label new GPU evidence separately; do not refresh historical hashes to hide a change.

Do not commit model weights, environment directories, raw responses, host identifiers, credentials or old evidence archives. Report empirical failures and negative results alongside success. New campaigns must use their own hardware/source identity. Contributions do not authorize accessing another person's machine, changing a running engine or publishing private evidence.
