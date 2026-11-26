# Contributing

Thanks for helping. This is an unofficial community tool, not affiliated with Anthropic. Issues and pull requests are welcome.

## Who can help most

Linux is the stable platform. macOS and Windows are beta, tested in CI only, and the author has no Mac or Windows machine. The most useful thing you can do is run it on one and report what happened.

* Run `python3 reach.py` and `python3 detect.py status` (use `python` on Windows).
* Open an issue with the "Platform report" template and paste the output. A report that says "it works" is useful too.
* If a route fails, a PR that fixes it is better still.

## Setup

You need Python 3.11 or newer. The tests use only the standard library. `psutil` and `tzdata` are used on macOS and Windows:

    python3 -m pip install psutil tzdata
    python3 -m unittest discover -s tests -v

## How the code is laid out

* `lib.py` holds the shared logic: reset parsing, transcript analysis, eligibility, delivery.
* `platform_linux.py`, `platform_macos.py` and `platform_windows.py` each hold one platform's process discovery, scheduling and delivery code.
* `platform_base.py` holds the interface, the psutil process source and the poll scheduler.

A platform object has a process source, a scheduler and a few path rules. Read the top of `platform_base.py` before you change one. Keep platform code out of `lib.py`, and keep the Linux code path as it is unless the change is about Linux.

## Rules for changes

* Never add typing into the focused window. A resume with no verified route is skipped on purpose.
* Every route must check that the target really hosts the `claude` process before it sends.
* A delivered message is never retried.
* Do not hard code socket or pipe paths. Read them from the session registry.
* Add a test with a fake for every new route or generator. Tests must not touch real sessions or real timers, except tests that are skipped unless the real OS tool is there (see `test_real_schtasks_accepts_the_one_time_task` for the pattern, with a clear skip reason).
* If a test cannot run on every OS, skip it with a reason that says why, for example `unittest.skipUnless(sys.platform == "win32", "needs real Windows named pipes")`.

## Pull requests

* Keep one change per pull request.
* The GitHub Actions run must pass on Ubuntu, macOS and Windows with Python 3.11 and 3.12.
* Do not paste real session ids, home paths, host names or tokens into issues, tests or fixtures. Use made up values like the ones in `tests/`.
* Write plain, direct English in docs. Short sentences. No dashes as punctuation.

## Reporting a bug

Use the "Bug report" template. Include the output of `reach.py` and `detect.py status`, and the last lines of the log (`auto-resume.log` in the data folder named on the first line of `reach.py`). Remove anything private first.
