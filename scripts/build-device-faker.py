#!/usr/bin/env python3
"""Build the pinned Device Faker source and its independent management process."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "components/device-faker"
SOURCE = ENGINE / "engine"
TARGET = ROOT / "target/device-faker"


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def check_source():
    if not (SOURCE / "Cargo.toml").is_file():
        raise ValueError("Embedded Device Faker source is missing")
    return (ENGINE / "UPSTREAM_REVISION").read_text().strip()


def prepare_native():
    source = TARGET / "source"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(SOURCE, source)
    manifest = source / "Cargo.toml"
    manifest.write_text(manifest.read_text().replace('crate-type = ["cdylib"]', 'crate-type = ["staticlib"]'))
    return source


def unlicensed_dependencies(metadata):
    missing = []
    for package in metadata["packages"]:
        if (package.get("license") or "").strip():
            continue
        license_file = package.get("license_file")
        if license_file:
            path = Path(package["manifest_path"]).parent / license_file
            if path.is_file() and path.read_bytes().strip():
                continue
        missing.append(f'{package["name"]}@{package["version"]}')
    return sorted(set(missing))


def notices(payload, source, commit):
    destination = ROOT / "module/licenses/device-faker"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(SOURCE / "LICENSE", destination / "GPL-3.0.txt")
    shutil.copy2(SOURCE / "NOTICES", destination / "NOTICE")
    shutil.copy2(ENGINE / "UPSTREAM_REVISION", destination / "UPSTREAM_REVISION")
    missing = set()
    for manifest in [source / "Cargo.toml", ENGINE / "manager/Cargo.toml"]:
        metadata = json.loads(subprocess.check_output(
            ["cargo", "metadata", "--locked", "--format-version=1", "--manifest-path", str(manifest)], cwd=ROOT,
        ))
        missing.update(unlicensed_dependencies(metadata))
        for package in metadata["packages"]:
            parent = Path(package["manifest_path"]).parent
            for path in parent.iterdir():
                if path.is_file() and path.name.upper().startswith(("LICENSE", "NOTICE", "COPYING")):
                    target = destination / "dependencies" / f'{package["name"]}-{package["version"]}' / path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            if package.get("license_file"):
                path = parent / package["license_file"]
                if path.is_file():
                    target = destination / "dependencies" / f'{package["name"]}-{package["version"]}' / "declared-license.txt"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
    (destination / "SOURCE.txt").write_text(
        f"Corresponding GPLv3 source and adaptations (Git commit {commit}):\n"
        f"https://github.com/eltavine/Duck-ToolBox/tree/{commit}/components/device-faker\n"
        f"Build script: https://github.com/eltavine/Duck-ToolBox/blob/{commit}/scripts/build-device-faker.py\n"
        "All engine source is included in this repository. Run the build script.\n"
    )
    return sorted(missing)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-only", action="store_true")
    parser.add_argument("--lint-native", action="store_true", help="Lint the native Android engine after building")
    parser.add_argument("--sdk", default=os.environ.get("ANDROID_HOME", str(Path.home() / "Library/Android/sdk")))
    parser.add_argument("--android-abi", choices=["arm64-v8a", "x86_64"], default="arm64-v8a")
    args = parser.parse_args()
    revision = check_source()
    marker = ROOT / "SOURCE_COMMIT"
    commit = marker.read_text().strip() if marker.is_file() else subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    base_env = {**os.environ, "DEVICE_FAKER_BUILD": commit}
    run("cargo", "build", "--locked", "--manifest-path", ENGINE / "manager/Cargo.toml", "--target-dir", ROOT / "target/device-faker-manager", env=base_env)
    if args.host_only:
        return
    ndk = Path(args.sdk) / "ndk/30.0.16248370"
    bins = list((ndk / "toolchains/llvm/prebuilt").glob("*/bin"))
    if not bins:
        raise ValueError("Install pinned Android NDK 30.0.16248370 first")
    toolchain = bins[0]
    suffix = ".cmd" if os.name == "nt" else ""
    target_triple = "aarch64-linux-android" if args.android_abi == "arm64-v8a" else "x86_64-linux-android"
    linker = toolchain / f"{target_triple}26-clang{suffix}"
    env_key = target_triple.upper().replace("-", "_")
    env = {**base_env,
           f"CARGO_TARGET_{env_key}_LINKER": str(linker),
           f"CC_{env_key}": str(linker),
           f"AR_{env_key}": str(toolchain / ("llvm-ar.exe" if os.name == "nt" else "llvm-ar"))}
    abi_suffix = "" if args.android_abi == "arm64-v8a" else f"-{args.android_abi}"
    native_dir = TARGET / f"native{abi_suffix}"
    manager_dir = ROOT / f"target/device-faker-manager{abi_suffix}"
    source = prepare_native()
    for manifest, target in [(source / "Cargo.toml", native_dir),
                             (ENGINE / "manager/Cargo.toml", manager_dir)]:
        run("cargo", "build", "--locked", "--release", "--manifest-path", manifest,
            "--target-dir", target, "--target", target_triple, env=env)
    if args.lint_native:
        run("cargo", "clippy", "--locked", "--manifest-path", source / "Cargo.toml",
            "--target-dir", native_dir, "--target", target_triple,
            "--", "-D", "warnings", env=env)
    payload = ROOT / ("module/device-faker" if args.android_abi == "arm64-v8a"
                      else "target/device-faker-x86_64")
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    static = TARGET / ("libdevicefaker.a" if args.android_abi == "arm64-v8a"
                       else "libdevicefaker-x86_64.a")
    objcopy = toolchain / ("llvm-objcopy.exe" if os.name == "nt" else "llvm-objcopy")
    run(objcopy, "--redefine-sym", "zygisk_module_entry=duck_faker_module_entry",
        "--redefine-sym", "zygisk_companion_entry=duck_faker_companion",
        native_dir / f"{target_triple}/release/libzygisk.a", static)
    shutil.copy2(manager_dir / f"{target_triple}/release/device-faker-engine", payload / "device-faker-engine")
    (payload / "device-faker-engine").chmod(0o755)
    missing = notices(payload, source, commit)
    files = [p for p in payload.rglob("*") if p.is_file()]
    inventory_root = ROOT / "module" if args.android_abi == "arm64-v8a" else ROOT
    (payload / "build.json").write_text(json.dumps({
        "upstream_revision": revision, "source_commit": commit,
        "unlicensed_dependencies": missing,
        "files": {p.relative_to(inventory_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
