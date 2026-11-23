#!/usr/bin/env python3
"""Check installer platform/API guards before any runtime preparation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallGuards(unittest.TestCase):
    def install(self, architecture, api, edition="duck-toolbox", conflict=False, backend=True):
        with tempfile.TemporaryDirectory(prefix="duck-install-guard-") as directory:
            root = Path(directory)
            shutil.copy2(ROOT / "module/customize.sh", root / "customize.sh")
            shutil.copy2(ROOT / "module/identity.sh", root / "identity.sh")
            if edition == "duck-toolbox":
                shutil.copy2(ROOT / "module/device-faker.sh", root / "device-faker.sh")
            (root / "module.prop").write_text(f"id={edition}\n")
            if backend:
                (root / "bin").mkdir()
                (root / "bin/duckd-x86_64").write_text("x86_64-backend\n")
            (root / "util_functions.sh").write_text('''
. "$MODPATH/identity.sh"
MODULE_ID=$(duck_module_id "$MODPATH")
DATA_ROOT="$TEST_RUNTIME"
conflicting_installation() { [ "$TEST_CONFLICT" = 1 ]; }
repair_runtime() { mkdir -p "$TEST_RUNTIME"; touch "$TEST_RUNTIME/prepared"; }
normalize_boot_hash() { :; }
''')
            runtime = root / "runtime"
            result = subprocess.run(["sh", "-c", '''
ui_print() { printf '%s\\n' "$*"; }
abort() { printf '%s\\n' "$*"; exit 1; }
. "$MODPATH/customize.sh"
'''], capture_output=True, text=True, timeout=10, env={
                **os.environ, "ARCH": architecture, "API": str(api), "KSU": "true",
                "APATCH": "false", "MODPATH": str(root), "TEST_RUNTIME": str(runtime),
                "TEST_CONFLICT": str(int(conflict)),
            })
            selected = root / "bin/duckd"
            return result, (runtime / "prepared").exists(), selected.read_text() if selected.is_file() else None

    def test_supported_arm64_prepares_runtime(self):
        result, prepared, backend = self.install("arm64", 26)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared)
        self.assertIsNone(backend)

    def test_supported_x86_64_prepares_runtime(self):
        for architecture in ("x64", "x86_64"):
            with self.subTest(architecture=architecture):
                result, prepared, backend = self.install(architecture, 26)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(prepared)
                self.assertEqual(backend, "x86_64-backend\n")
                self.assertNotIn("Device Faker", result.stdout)

    def test_x86_64_without_backend_fails_before_runtime_changes(self):
        result, prepared, backend = self.install("x64", 26, backend=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("x86_64 backend missing", result.stdout)
        self.assertFalse(prepared)
        self.assertIsNone(backend)

    def test_incompatible_architectures_fail_before_runtime_changes(self):
        for architecture in ["x86", "arm", "unknown"]:
            with self.subTest(architecture=architecture):
                result, prepared, _ = self.install(architecture, 26)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("requires arm64 or x86_64", result.stdout)
                self.assertFalse(prepared)

    def test_unsupported_android_fails_before_runtime_changes(self):
        for architecture in ("arm64", "x64"):
            with self.subTest(architecture=architecture):
                result, prepared, _ = self.install(architecture, 25)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("API 26", result.stdout)
                self.assertFalse(prepared)

    def test_lite_installs_without_device_faker_setup(self):
        for architecture in ("arm64", "x64"):
            with self.subTest(architecture=architecture):
                result, prepared, _ = self.install(architecture, 26, "duck-toolbox-lite")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(prepared)
                self.assertNotIn("Device Faker", result.stdout)

    def test_other_edition_blocks_install_before_runtime_changes(self):
        for architecture in ("arm64", "x64"):
            for edition in ("duck-toolbox", "duck-toolbox-lite"):
                with self.subTest(architecture=architecture, edition=edition):
                    result, prepared, _ = self.install(architecture, 26, edition, conflict=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("cannot coexist", result.stdout)
                    self.assertFalse(prepared)

    def test_active_and_pending_other_editions_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for parent in ("modules", "modules_update"):
                for edition, other in (("duck-toolbox", "duck-toolbox-lite"),
                                       ("duck-toolbox-lite", "duck-toolbox")):
                    with self.subTest(parent=parent, edition=edition):
                        path = root / parent / other
                        path.mkdir(parents=True, exist_ok=True)
                        args = ["sh", "-c", '. "$1"; conflicting_installation "$2" "$3"',
                                "sh", str(ROOT / "module/identity.sh"), edition, directory]
                        self.assertEqual(subprocess.run(args, timeout=10).returncode, 0)
                        path.rmdir()
                        self.assertEqual(subprocess.run(args, timeout=10).returncode, 1)

    def test_edition_lookup_refuses_missing_or_wrong_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "duck-toolbox-lite"
            module.mkdir()
            args = ["sh", "-c", '. "$1"; duck_module_id "$2"',
                    "sh", str(ROOT / "module/identity.sh"), str(module)]
            for body in (None, "id=duck-toolbox\n", "id=duck-toolbox-lite\nid=duck-toolbox-lite\n"):
                if body is not None:
                    (module / "module.prop").write_text(body)
                result = subprocess.run(args, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
            (module / "module.prop").write_text("id=duck-toolbox-lite\n")
            self.assertEqual(subprocess.check_output(args, timeout=10), b"duck-toolbox-lite\n")


if __name__ == "__main__":
    unittest.main()
