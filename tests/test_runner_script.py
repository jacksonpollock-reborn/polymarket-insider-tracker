import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "scripts" / "run_tracker.sh"


class RunnerScriptTests(unittest.TestCase):
    def _write_fake_main(self, repo_root: pathlib.Path) -> None:
        main_py = repo_root / "main.py"
        main_py.write_text(
            textwrap.dedent(
                """
                import json
                import os
                import pathlib
                import sys

                state_path = pathlib.Path("attempt_count.txt")
                attempt = int(state_path.read_text()) + 1 if state_path.exists() else 1
                state_path.write_text(str(attempt))

                mode = os.environ["TEST_RUNNER_MODE"]

                def write_watchlist(status, reason=None, last_error=None):
                    payload = {
                        "run_health": {
                            "status": status,
                            "reason": reason,
                            "request_health": {
                                "successful_calls": 0,
                                "attempt_failures": 0,
                                "failed_calls": 0,
                                "last_error": last_error,
                            },
                        }
                    }
                    pathlib.Path("watchlist.json").write_text(json.dumps(payload))

                if mode == "dns_then_success":
                    if attempt == 1:
                        write_watchlist(
                            "unhealthy",
                            reason="market_bootstrap_failed: Failed to resolve gamma-api.polymarket.com",
                            last_error="Failed to resolve gamma-api.polymarket.com",
                        )
                        sys.exit(1)
                    write_watchlist("healthy")
                    sys.exit(0)

                if mode == "smtp_dns_then_success":
                    if attempt == 1:
                        write_watchlist("healthy")
                        print("Failed to send email: [Errno 8] nodename nor servname provided, or not known")
                        sys.exit(1)
                    write_watchlist("healthy")
                    sys.exit(0)

                write_watchlist("healthy")
                print("unexpected failure")
                sys.exit(1)
                """
            ).strip()
            + "\n"
        )

    def _write_fake_osascript(self, bin_dir: pathlib.Path) -> None:
        osascript_path = bin_dir / "osascript"
        osascript_path.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" >> \"$TEST_OSASCRIPT_LOG\"\n"
            "cat >/dev/null\n"
            "exit 0\n"
        )
        osascript_path.chmod(osascript_path.stat().st_mode | stat.S_IXUSR)

    def _make_temp_repo(self) -> tempfile.TemporaryDirectory:
        tempdir = tempfile.TemporaryDirectory()
        repo_root = pathlib.Path(tempdir.name)
        (repo_root / "scripts").mkdir()
        shutil.copy(RUNNER_PATH, repo_root / "scripts" / "run_tracker.sh")
        (repo_root / "scripts" / "run_tracker.sh").chmod(
            (repo_root / "scripts" / "run_tracker.sh").stat().st_mode | stat.S_IXUSR
        )
        self._write_fake_main(repo_root)
        bin_dir = repo_root / "bin"
        bin_dir.mkdir()
        self._write_fake_osascript(bin_dir)
        return tempdir

    def test_runner_retries_after_dns_unhealthy_run(self):
        tempdir = self._make_temp_repo()
        self.addCleanup(tempdir.cleanup)
        repo_root = pathlib.Path(tempdir.name)
        env = os.environ.copy()
        env.update(
            {
                "TEST_RUNNER_MODE": "dns_then_success",
                "TRACKER_RETRY_DELAY_SECONDS": "0",
                "TEST_OSASCRIPT_LOG": str(repo_root / "osascript.log"),
                "PATH": f"{repo_root / 'bin'}:{env['PATH']}",
            }
        )

        result = subprocess.run(
            ["zsh", str(repo_root / "scripts" / "run_tracker.sh")],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual((repo_root / "attempt_count.txt").read_text().strip(), "2")

    def test_runner_reexecs_under_zsh_when_invoked_with_bash(self):
        tempdir = self._make_temp_repo()
        self.addCleanup(tempdir.cleanup)
        repo_root = pathlib.Path(tempdir.name)
        env = os.environ.copy()
        env.update(
            {
                "TEST_RUNNER_MODE": "dns_then_success",
                "TRACKER_RETRY_DELAY_SECONDS": "0",
                "TEST_OSASCRIPT_LOG": str(repo_root / "osascript.log"),
                "PATH": f"{repo_root / 'bin'}:{env['PATH']}",
            }
        )

        result = subprocess.run(
            ["bash", str(repo_root / "scripts" / "run_tracker.sh")],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual((repo_root / "attempt_count.txt").read_text().strip(), "2")

    def test_runner_retries_after_smtp_dns_failure(self):
        tempdir = self._make_temp_repo()
        self.addCleanup(tempdir.cleanup)
        repo_root = pathlib.Path(tempdir.name)
        env = os.environ.copy()
        env.update(
            {
                "TEST_RUNNER_MODE": "smtp_dns_then_success",
                "TRACKER_RETRY_DELAY_SECONDS": "0",
                "TEST_OSASCRIPT_LOG": str(repo_root / "osascript.log"),
                "PATH": f"{repo_root / 'bin'}:{env['PATH']}",
            }
        )

        result = subprocess.run(
            ["zsh", str(repo_root / "scripts" / "run_tracker.sh")],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual((repo_root / "attempt_count.txt").read_text().strip(), "2")

    def test_runner_does_not_retry_non_dns_failure(self):
        tempdir = self._make_temp_repo()
        self.addCleanup(tempdir.cleanup)
        repo_root = pathlib.Path(tempdir.name)
        env = os.environ.copy()
        env.update(
            {
                "TEST_RUNNER_MODE": "generic_failure",
                "TRACKER_RETRY_DELAY_SECONDS": "0",
                "TEST_OSASCRIPT_LOG": str(repo_root / "osascript.log"),
                "PATH": f"{repo_root / 'bin'}:{env['PATH']}",
            }
        )

        result = subprocess.run(
            ["zsh", str(repo_root / "scripts" / "run_tracker.sh")],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertEqual((repo_root / "attempt_count.txt").read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()
